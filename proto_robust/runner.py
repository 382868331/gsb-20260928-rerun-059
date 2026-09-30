from __future__ import annotations

from importlib import import_module
import multiprocessing as mp
import os
import re
import time
import traceback
from typing import Any, Callable

from proto_robust.models import Outcome, ParseReject, TargetResult


def load_callable(dotted_name: str) -> Callable[[bytes], Any]:
    module_name, function_name = dotted_name.rsplit(".", 1)
    function = getattr(import_module(module_name), function_name)
    if not callable(function):
        raise TypeError(f"{dotted_name} is not callable")
    return function


def raise_site(exc: BaseException, target_module: str) -> str:
    """Return a stable location tag for the deepest frame inside the target module."""
    target_file = None
    module = None
    try:
        module = import_module(target_module)
        target_file = os.path.normcase(getattr(module, "__file__", "") or "")
    except Exception:
        target_file = ""
    fallback = f"{type(exc).__name__}"
    frames = traceback.extract_tb(exc.__traceback__)
    for frame in reversed(frames):
        if target_file and frame.filename and os.path.normcase(frame.filename) == target_file:
            return f"{os.path.basename(frame.filename)}:{frame.lineno}"
    if frames:
        frame = frames[-1]
        return f"{os.path.basename(frame.filename)}:{frame.lineno}"
    return fallback


def failure_condition(exception_type: str, message: str) -> str:
    """Derive a stable key-condition fingerprint from an exception message.

    For boundary failures the concrete offending index identifies the
    trigger; reducing a sample must keep that same condition, not merely
    raise the same exception somewhere on the same line.
    """
    if not message:
        return ""
    lowered = message.lower()
    numbers = re.findall(r"-?\d+", lowered)
    if "index" in lowered and numbers:
        return f"index:{numbers[0]}"
    if "key" in lowered and numbers:
        return f"key:{numbers[0]}"
    # Fallback: a short stable token of the message shape.
    return re.sub(r"\s+", " ", lowered.strip())[:80]


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
    module_name = target_name.rsplit(".", 1)[0]
    while True:
        try:
            data = conn.recv()
        except (EOFError, OSError):
            return
        try:
            target(data)
        except ParseReject as exc:
            result = TargetResult(
                Outcome.REJECTED,
                0.0,
                type(exc).__name__,
                str(exc),
                site=raise_site(exc, module_name),
                condition=failure_condition(type(exc).__name__, str(exc)),
            )
        except Exception as exc:
            result = TargetResult(
                Outcome.CRASH,
                0.0,
                type(exc).__name__,
                str(exc),
                site=raise_site(exc, module_name),
                condition=failure_condition(type(exc).__name__, str(exc)),
            )
        else:
            result = TargetResult(Outcome.OK, 0.0)
        try:
            conn.send(result)
        except (BrokenPipeError, OSError):
            return


def _worker(conn, target: Callable[[bytes], Any], data: bytes) -> None:
    module_name = getattr(target, "__module__", "")
    try:
        target(data)
    except ParseReject as exc:
        conn.send(
            TargetResult(
                Outcome.REJECTED,
                0.0,
                type(exc).__name__,
                str(exc),
                site=raise_site(exc, module_name),
                condition=failure_condition(type(exc).__name__, str(exc)),
            )
        )
    except Exception as exc:  # Defects are reported, not raised through the harness.
        conn.send(
            TargetResult(
                Outcome.CRASH,
                0.0,
                type(exc).__name__,
                str(exc),
                site=raise_site(exc, module_name),
                condition=failure_condition(type(exc).__name__, str(exc)),
            )
        )
    else:
        conn.send(TargetResult(Outcome.OK, 0.0))
    finally:
        conn.close()
