"""Fresh-process replay of multi-step session sequences.

Every sequence replay starts a brand new worker process.  Therefore a worker
that timed out, was explicitly rejected mid-session or crashed can never carry
its polluted session state into the next sequence; the next sequence gets a
clean session starting point by construction.
"""

from __future__ import annotations

import multiprocessing as mp
import time
from typing import Any

from proto_robust.models import Outcome, ParseReject, SessionPreparationError
from proto_robust.runner import _fault_location, load_callable
from proto_robust.sequences import Interaction, ReplayResult, SessionSequence, StepRecord


def _send_step(conn, target_replay: Any, session: dict, interaction: Interaction,
                step: int, attempt_namespace: str) -> tuple:
    start = time.monotonic()
    try:
        target_replay(interaction.data, session, attempt_namespace)
    except SessionPreparationError as exc:
        file_name, function_name, line_number = _fault_location(exc)
        record = {
            "kind": Outcome.PREPARATION_ERROR.value,
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "file": file_name,
            "function": function_name,
            "line": line_number,
            "state_path": "",
            "keys": (),
        }
    except ParseReject as exc:
        file_name, function_name, line_number = _fault_location(exc)
        record = {
            "kind": Outcome.REJECTED.value,
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "file": file_name,
            "function": function_name,
            "line": line_number,
            "state_path": "",
            "keys": (),
        }
    except Exception as exc:
        file_name, function_name, line_number = _fault_location(exc)
        state_path, keys = _session_fault_key(exc, file_name, line_number, step)
        record = {
            "kind": Outcome.CRASH.value,
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "file": file_name,
            "function": function_name,
            "line": line_number,
            "state_path": state_path,
            "keys": keys,
        }
    else:
        record = {"kind": Outcome.OK.value}
    record["duration"] = time.monotonic() - start
    return record, session


def _session_worker(conn, replay_name: str, describe_name: str,
                    steps: list[Interaction], attempt_namespace: str) -> None:
    replay = load_callable(replay_name)
    describe = load_callable(describe_name) if describe_name else (lambda session: session.get("state", "closed"))
    session: dict = {
        "state": "closed",
        "mode": None,
        "bucket": None,
        "stall": False,
        "flaky_calls": 0,
    }
    start_state = describe(session)
    conn.send({"type": "start", "state": start_state})
    for step, interaction in enumerate(steps):
        state_before = describe(session)
        record, session = _send_step(conn, replay, session, interaction, step, attempt_namespace)
        state_after = describe(session)
        conn.send({
            "type": "step",
            "step": step,
            "name": interaction.name,
            "hex": interaction.data.hex(),
            "state_before": state_before,
            "state_after": state_after,
            "record": record,
        })
        if record["kind"] != Outcome.OK.value:
            return
    conn.send({"type": "done", "state": describe(session)})


def replay_sequence_isolated(
    sequence: SessionSequence,
    replay_name: str,
    describe_name: str,
    timeout: float,
    attempt_namespace: str = "confirm",
) -> ReplayResult:
    """Replay one full sequence in a freshly spawned worker process."""
    context = mp.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(
        target=_session_worker,
        args=(child, replay_name, describe_name, list(sequence.steps), attempt_namespace),
    )
    start = time.monotonic()
    process.start()
    child.close()

    result = ReplayResult(
        outcome=Outcome.OK.value,
        start_state=sequence.start_state,
        end_state=sequence.start_state,
        duration=0.0,
    )
    overall = Outcome.OK
    timed_out = False
    messages: list[dict] = []

    eof = False

    def wait_message(remaining: float) -> dict | None:
        nonlocal eof
        if parent.poll(max(0.0, remaining)):
            try:
                return parent.recv()
            except EOFError:
                eof = True
                return None
        return None

    total_steps = len(sequence.steps)
    for expected in range(total_steps + 1):
        elapsed = time.monotonic() - start
        message = wait_message(timeout - elapsed)
        if message is None:
            if not eof:
                timed_out = True
            break
        messages.append(message)
        if message["type"] == "start":
            result.start_state = message.get("state", result.start_state)
            continue
        if message["type"] == "step":
            record = message["record"]
            step_record = StepRecord(
                step=message["step"],
                name=message["name"],
                hex=message["hex"],
                outcome=record["kind"],
                exception_type=record.get("exception_type", ""),
                message=record.get("message", ""),
                state_before=message.get("state_before", ""),
                state_after=message.get("state_after", ""),
                duration=record.get("duration", 0.0),
            )
            result.steps.append(step_record)
            kind = record["kind"]
            if kind != Outcome.OK.value:
                overall = Outcome(kind)
                result.exception_type = record.get("exception_type", "")
                result.message = record.get("message", "")
                result.fault_file = record.get("file", "")
                result.fault_function = record.get("function", "")
                result.fault_line = record.get("line", 0)
                result.state_path = record.get("state_path", "")
                result.key_conditions = tuple(record.get("keys", ()))
                result.failing_step = message["step"]
                result.end_state = message.get("state_before", "")
                break
        if message["type"] == "done":
            result.end_state = message.get("state", result.end_state)
            break

    process.join(max(0.0, timeout - (time.monotonic() - start)))
    if process.is_alive():
        process.terminate()
        process.join(0.2)
        if process.is_alive():
            process.kill()
            process.join()
    parent.close()
    result.duration = time.monotonic() - start

    if timed_out:
        # Record the interaction that was in flight when the deadline passed.
        observed_steps = len(result.steps)
        if observed_steps < len(sequence.steps):
            pending = sequence.steps[observed_steps]
            state_before = result.steps[-1].state_after if result.steps else sequence.start_state
            result.steps.append(StepRecord(
                step=observed_steps,
                name=pending.name,
                hex=pending.data.hex(),
                outcome=Outcome.TIMEOUT.value,
                exception_type="Timeout",
                message="interaction exceeded the per-replay time budget",
                state_before=state_before,
                state_after=state_before,
                duration=result.duration,
            ))
            result.failing_step = observed_steps
            result.end_state = state_before
        result.outcome = Outcome.TIMEOUT.value
        result.message = "sequence exceeded per-replay time budget"
        return result
    if eof and overall is Outcome.OK:
        # The worker disappeared (e.g. os._exit/abort) without a Python result.
        process.join(0.1)
        observed_steps = len(result.steps)
        if observed_steps < len(sequence.steps):
            pending = sequence.steps[observed_steps]
            state_before = result.steps[-1].state_after if result.steps else sequence.start_state
            result.steps.append(StepRecord(
                step=observed_steps,
                name=pending.name,
                hex=pending.data.hex(),
                outcome=Outcome.RUNNER_ERROR.value,
                exception_type="AbnormalProcessExit",
                message=f"worker exited with {process.exitcode}",
                state_before=state_before,
                state_after=state_before,
                duration=result.duration,
            ))
        result.outcome = Outcome.RUNNER_ERROR.value
        result.exception_type = "AbnormalProcessExit"
        result.message = f"worker exited with {process.exitcode}"
        result.exit_code = process.exitcode
        if result.steps:
            last = result.steps[-1]
            result.failing_step = last.step
            result.end_state = last.state_before
        else:
            result.failing_step = 0
            result.end_state = result.start_state
        return result
    if overall is Outcome.OK and process.exitcode not in (0, None):
        # The worker vanished (native abort) after the last observed step.
        result.outcome = Outcome.RUNNER_ERROR.value
        result.exception_type = "AbnormalProcessExit"
        result.message = f"worker exited with {process.exitcode}"
        result.exit_code = process.exitcode
        if result.steps:
            last = result.steps[-1]
            result.failing_step = last.step
            result.end_state = last.state_before
        return result
    result.outcome = overall.value
    result.exit_code = process.exitcode
    return result


def _session_fault_key(exc: BaseException, file_name: str, line_number: int, step: int) -> tuple[str, tuple]:
    """Extract the session state path and structural key bytes of a defect."""
    if not file_name.endswith("session_store.py"):
        return "", ()
    tb = exc.__traceback__
    frame = None
    while tb is not None:
        if tb.tb_lineno == line_number:
            frame = tb.tb_frame
        tb = tb.tb_next
    if frame is None:
        return "", ()
    session = frame.f_locals.get("session")
    data = frame.f_locals.get("data")
    if not isinstance(session, dict):
        return "", ()
    state = str(session.get("state", ""))
    mode = session.get("mode")
    bucket = session.get("bucket")
    parts = [state]
    if mode:
        parts.append(f"mode={mode}")
    if bucket:
        parts.append(f"bucket={bucket}")
    state_path = "/".join(parts)
    keys: list[tuple[int, int, int]] = []
    if isinstance(data, (bytes, bytearray)) and len(data) >= 2:
        command, key = data[0], data[1]
        keys.append((step, 0, command))
        keys.append((step, 1, key))
    return state_path, tuple(keys)
