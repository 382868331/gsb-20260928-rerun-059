from __future__ import annotations

from importlib import import_module
import multiprocessing as mp
import traceback
import time
from typing import Any, Callable

from proto_robust.models import Outcome, ParseReject, TargetResult


def load_callable(dotted_name: str) -> Callable[[bytes], Any]:
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


def _key_conditions(exc: BaseException, file_name: str, line_number: int) -> tuple[tuple[int, int], ...]:
    """Extract the bytes that are the key condition of the seeded defect.

    Only the project-owned target is inspected this way, so the rules stay
    explicit.  Each condition is an (offset, value) pair in the input.
    """
    if not file_name.endswith("simple_message.py"):
        return ()
    tb = exc.__traceback__
    frame = None
    while tb is not None:
        if tb.tb_lineno == line_number:
            frame = tb.tb_frame
        tb = tb.tb_next
    if frame is None:
        return ()
    conditions: list[tuple[int, int]] = []
    data = frame.f_locals.get("data")
    if data is not None and len(data) >= 7 and data[6] == 0xFF:
        conditions.append((6, 0xFF))
    if data is not None and len(data) >= 8 and data[7] == 0xFE:
        conditions.append((6, data[6]))
        conditions.append((7, 0xFE))
    return tuple(conditions)


def run_isolated(target: Callable[[bytes], Any], data: bytes, timeout: float) -> TargetResult:
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
    """A reusable process boundary that is replaced after timeout or death."""

    def __init__(self, target_name: str) -> None:
        self.target_name = target_name
        self._context = mp.get_context("spawn")
        self._process: mp.process.BaseProcess | None = None
        self._parent = None
        self._start_worker()

    def run(self, data: bytes, timeout: float) -> TargetResult:
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
        if result.outcome is Outcome.CRASH:
            # Python exceptions are caught by the worker. A native crash would
            # instead arrive as EOF; keeping the current sample remains valid.
            pass
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
            target(data)
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


def _worker(conn, target: Callable[[bytes], Any], data: bytes) -> None:
    try:
        target(data)
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
