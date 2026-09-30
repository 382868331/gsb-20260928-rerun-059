from __future__ import annotations

from importlib import import_module
import multiprocessing as mp
import traceback
import time
from typing import Any, Callable

from proto_robust.models import Outcome, ParseReject, SessionSequence, TargetResult

FAILURE_OUTCOMES = {Outcome.CRASH, Outcome.TIMEOUT, Outcome.RUNNER_ERROR}
RESTART_OUTCOMES = FAILURE_OUTCOMES | {Outcome.REJECTED}


def load_callable(dotted_name: str) -> Callable[[Any], Any]:
    module_name, function_name = dotted_name.rsplit(".", 1)
    function = getattr(import_module(module_name), function_name)
    if not callable(function):
        raise TypeError(f"{dotted_name} is not callable")
    return function


def _fault_location(exc: BaseException) -> tuple[str, str, int]:
    """Return (file, function, line) of the deepest user-code frame in the traceback."""
    tb = exc.__traceback__
    file_name = function_name = ""
    line_number = 0
    while tb is not None:
        frame = tb.tb_frame
        module_file = frame.f_code.co_filename or ""
        normalized = module_file.replace(chr(92), "/")
        if not normalized.endswith("proto_robust/runner.py"):
            file_name = module_file
            function_name = frame.f_code.co_name
            line_number = tb.tb_lineno
        tb = tb.tb_next
    if not function_name:
        extracted = traceback.extract_tb(exc.__traceback__)
        if extracted:
            frame_summary = extracted[-1]
            file_name = frame_summary.filename or ""
            function_name = frame_summary.name or ""
            line_number = frame_summary.lineno or 0
    return file_name, function_name, line_number


def _key_conditions(exc: BaseException, file_name: str, line_number: int) -> tuple[tuple[int, int, int], ...]:
    """Extract the bytes that are the key condition of the seeded defect.

    Only the project-owned target is inspected this way, so the rules stay
    explicit. Each condition is a (step, offset, value) triple in the sequence.
    """
    if not file_name.endswith("simple_message.py"):
        return ()
    tb = exc.__traceback__
    frames = []
    while tb is not None:
        normalized = (tb.tb_frame.f_code.co_filename or "").replace(chr(92), "/")
        if normalized.endswith("simple_message.py"):
            frames.append(tb.tb_frame)
        tb = tb.tb_next
    if not frames:
        return ()
    frame = next((candidate for candidate in reversed(frames) if candidate.f_lineno == line_number), frames[-1])
    sequence_frame = next((candidate for candidate in reversed(frames) if "step_index" in candidate.f_locals), None)
    step_index = int(sequence_frame.f_locals["step_index"]) if sequence_frame is not None else -1
    conditions: list[tuple[int, int, int]] = []
    data = frame.f_locals.get("data") or frame.f_locals.get("message")
    if data is None and sequence_frame is not None:
        data = sequence_frame.f_locals.get("data") or sequence_frame.f_locals.get("message")
    if data is not None and len(data) >= 7 and data[6] == 0xFF:
        conditions.append((-1, 6, 0xFF))
    if data is not None and len(data) >= 8 and data[7] == 0xFE:
        conditions.append((-1, 6, data[6]))
        conditions.append((-1, 7, 0xFE))
    session = frame.f_locals.get("self")
    if session is None and sequence_frame is not None:
        session = sequence_frame.f_locals.get("self")
    if session is not None and getattr(session, "armed", False):
        arm_message = getattr(session, "arm_message", b"")
        if len(arm_message) >= 7:
            conditions.append((0, 6, arm_message[6]))
        if data is not None and len(data) >= 7:
            conditions.append((-1, 6, data[6]))
    return tuple(conditions)


def _execute_target(target: Callable[[Any], Any], payload: bytes | SessionSequence) -> Any:
    if isinstance(payload, SessionSequence):
        return target(payload)
    return target(payload)


def run_isolated(target: Callable[[Any], Any], data: bytes | SessionSequence, timeout: float) -> TargetResult:
    context = mp.get_context("spawn")
    parent_conn, child_conn = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(child_conn, target, data))
    start = time.monotonic()
    process.start()
    child_conn.close()
    result: TargetResult | None = None
    if parent_conn.poll(timeout):
        try:
            result = parent_conn.recv()
        except EOFError:
            result = None
    process.join(max(0.0, timeout - (time.monotonic() - start)))
    if process.is_alive():
        process.terminate()
        process.join(0.2)
        if process.is_alive():
            process.kill()
            process.join()
    parent_conn.close()
    duration = time.monotonic() - start
    if result is not None:
        result.duration = duration
        return result
    if process.exitcode is None:
        return TargetResult(Outcome.TIMEOUT, duration, exit_code=process.exitcode)
    if process.exitcode == 0:
        return TargetResult(Outcome.RUNNER_ERROR, duration, message="worker exited without result")
    return TargetResult(
        Outcome.RUNNER_ERROR,
        duration,
        exception_type="AbnormalProcessExit",
        message=f"worker exited with {process.exitcode}",
        exit_code=process.exitcode,
    )


class IsolatedWorker:
    """A process boundary replaced after timeout, rejection, crash or death."""

    def __init__(self, target_name: str) -> None:
        self.target_name = target_name
        self._context = mp.get_context("spawn")
        self._process: mp.process.BaseProcess | None = None
        self._parent = None
        self._start_worker()

    def run(self, data: bytes | SessionSequence, timeout: float) -> TargetResult:
        if self._process is None or not self._process.is_alive() or self._parent is None:
            self._start_worker()
        start = time.monotonic()
        try:
            self._parent.send(data)
        except (BrokenPipeError, OSError):
            result = TargetResult(
                Outcome.RUNNER_ERROR,
                time.monotonic() - start,
                "WorkerDisconnected",
                "worker process died before sample execution",
            )
            self._close_current()
            return result
        if not self._parent.poll(timeout):
            self._close_current()
            return TargetResult(Outcome.TIMEOUT, timeout, "Timeout", "worker exceeded time budget")
        try:
            result = self._parent.recv()
        except (EOFError, OSError):
            self._process.join(0.1)
            result = TargetResult(
                Outcome.RUNNER_ERROR,
                time.monotonic() - start,
                "AbnormalProcessExit",
                f"worker exited with {self._process.exitcode if self._process else None}",
            )
            self._close_current()
            return result
        result.duration = time.monotonic() - start
        if result.outcome in RESTART_OUTCOMES:
            self._close_current()
        return result

    def close(self) -> None:
        self._close_current()

    def _start_worker(self) -> None:
        self._close_current()
        self._parent, child = self._context.Pipe(duplex=True)
        self._process = self._context.Process(target=_persistent_worker, args=(child, self.target_name))
        self._process.start()
        child.close()

    def _close_current(self) -> None:
        if self._process is not None and self._process.is_alive():
            self._process.terminate()
            self._process.join(0.2)
            if self._process.is_alive():
                self._process.kill()
                self._process.join()
        self._process = None
        if self._parent is not None:
            self._parent.close()
            self._parent = None


def _persistent_worker(conn, target_name: str) -> None:
    target = load_callable(target_name)
    while True:
        try:
            data = conn.recv()
        except (EOFError, OSError):
            return
        try:
            _execute_target(target, data)
        except ParseReject as exc:
            file_name, function_name, line_number = _fault_location(exc)
            result = TargetResult(
                Outcome.REJECTED, 0.0, type(exc).__name__, str(exc),
                fault_file=file_name, fault_function=function_name, fault_line=line_number,
            )
        except Exception as exc:
            file_name, function_name, line_number = _fault_location(exc)
            conditions = _key_conditions(exc, file_name, line_number)
            result = TargetResult(
                Outcome.CRASH, 0.0, type(exc).__name__, str(exc),
                fault_file=file_name, fault_function=function_name, fault_line=line_number,
                key_conditions=conditions,
            )
        else:
            result = TargetResult(Outcome.OK, 0.0)
        try:
            conn.send(result)
        except (BrokenPipeError, OSError):
            return


def _worker(conn, target: Callable[[Any], Any], data: bytes | SessionSequence) -> None:
    try:
        _execute_target(target, data)
    except ParseReject as exc:
        file_name, function_name, line_number = _fault_location(exc)
        conn.send(TargetResult(
            Outcome.REJECTED, 0.0, type(exc).__name__, str(exc),
            fault_file=file_name, fault_function=function_name, fault_line=line_number,
        ))
    except Exception as exc:  # Defects are reported, not raised through the harness.
        file_name, function_name, line_number = _fault_location(exc)
        conditions = _key_conditions(exc, file_name, line_number)
        conn.send(TargetResult(
            Outcome.CRASH, 0.0, type(exc).__name__, str(exc),
            fault_file=file_name, fault_function=function_name, fault_line=line_number,
            key_conditions=conditions,
        ))
    else:
        conn.send(TargetResult(Outcome.OK, 0.0))
    finally:
        conn.close()
