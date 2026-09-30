from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from proto_robust.models import FailureSignature, Finding, Outcome, ReductionStep, TargetResult

Predicate = Callable[[bytes], TargetResult]


@dataclass
class KeepDecision:
    matches: bool
    hits: int
    trials: int


def reduce_finding(
    finding: Finding,
    run: Predicate,
    *,
    flaky: bool,
    confirm_trials: int,
    threshold: float = 0.5,
    on_progress: Callable[[Finding], None] | None = None,
) -> None:
    """Reduce a finding in place, preserving site and key condition.

    Resumable: an unfinished finding restarts from its current minimized
    candidate and appends fresh reasons to the existing step log.
    """
    if finding.reduction_done:
        return
    candidate = bytearray(finding.minimized)
    attempts = 0

    def keeps_failure(value: bytes | bytearray, phase: str, action: str) -> KeepDecision:
        nonlocal attempts
        trials_n = confirm_trials if flaky else 1
        hits = 0
        outcomes: list[str] = []
        for _ in range(trials_n):
            attempts += 1
            result = run(bytes(value))
            outcomes.append(
                f"{result.outcome.value}:{result.exception_type}@{result.site}#{result.condition}"
            )
            if _same_failure(result, finding.signature):
                hits += 1
        decision = KeepDecision(False, hits, trials_n)
        if hits:
            decision.matches = hits / trials_n >= threshold
        reason = (
            f"kept: {finding.signature.key()} reproduced {hits}/{trials_n} at original site"
            if decision.matches
            else f"rejected: original failure+site reproduced only {hits}/{trials_n} (" + ", ".join(outcomes) + ")"
        )
        finding.reduction_steps.append(
            ReductionStep(phase, action, decision.matches, reason, bytes(value).hex(), hits, trials_n)
        )
        return decision

    changed = True
    while changed:
        changed = False
        for position in range(len(candidate) - 1, 0, -1):
            trial = candidate[:position]
            if len(trial) >= 6:
                trial = _repair_sm_length(trial)
                decision = keeps_failure(trial, "prefix-trim", f"drop bytes after offset {position}")
                if decision.matches:
                    candidate = bytearray(trial)
                    finding.minimized = bytes(candidate)
                    if on_progress is not None:
                        on_progress(finding)
                    changed = True
                    break
        if changed:
            continue
        for position, value in enumerate(candidate):
            if position in (0, 1, 2, 4, 5):
                continue
            for replacement in (0, 1, 2, 4, 255):
                if replacement >= value:
                    continue
                trial = bytearray(candidate)
                trial[position] = replacement
                repaired = _repair_sm_length(trial)
                decision = keeps_failure(
                    repaired, "byte-replace", f"offset {position}: {value} -> {replacement}"
                )
                if decision.matches:
                    candidate = bytearray(repaired)
                    finding.minimized = bytes(candidate)
                    if on_progress is not None:
                        on_progress(finding)
                    changed = True
                    break
            if changed:
                break
    finding.minimized = bytes(candidate)
    finding.critical_byte_offsets = _critical_offsets(
        finding, finding.minimized, run, flaky, confirm_trials, threshold
    )
    finding.attempts += attempts
    finding.reduction_done = True


def reduce_case(
    data: bytes,
    signature: FailureSignature,
    run: Predicate,
    *,
    flaky: bool = False,
    confirm_trials: int | None = None,
    threshold: float = 0.5,
) -> tuple[bytes, int, list[int]]:
    """Backward-compatible one-shot reduction helper."""
    from proto_robust.models import ReproEvidence

    finding = Finding(signature, data, data, -1, 0)
    reduce_finding(
        finding,
        run,
        flaky=flaky,
        confirm_trials=confirm_trials if confirm_trials is not None else (4 if flaky else 1),
        threshold=threshold,
    )
    return finding.minimized, finding.attempts, finding.critical_byte_offsets


def _critical_offsets(
    finding: Finding,
    data: bytes,
    run: Predicate,
    flaky: bool,
    confirm_trials: int,
    threshold: float,
) -> list[int]:
    offsets: list[int] = []
    for position in range(len(data)):
        trial = bytearray(data)
        trial[position] = 0
        repaired = _repair_sm_length(trial)
        trials_n = confirm_trials if flaky else 1
        hits = sum(1 for _ in range(trials_n) if _same_failure(run(bytes(repaired)), finding.signature))
        if not hits or hits / trials_n < threshold:
            finding.attempts += trials_n
            offsets.append(position)
        else:
            finding.attempts += trials_n
    return offsets


def _repair_sm_length(data: bytes | bytearray) -> bytes:
    if len(data) < 6 or bytes(data[:2]) != b"SM":
        return bytes(data)
    payload_length = len(data) - 6
    return b"SM" + bytes([data[2], data[3]]) + payload_length.to_bytes(2, "big") + bytes(data[6:])


def _same_failure(result: TargetResult, expected: FailureSignature) -> bool:
    if result.outcome != expected.outcome:
        return False
    if expected.outcome is Outcome.CRASH:
        if result.exception_type != expected.exception_type:
            return False
        # Same exception type is not enough: the raise site identifies the
        # defect, so reducing one crash into another IndexError is rejected.
        if expected.site and result.site and result.site != expected.site:
            return False
        if expected.condition and result.condition and result.condition != expected.condition:
            return False
        return True
    if expected.outcome is Outcome.TIMEOUT:
        return True
    if expected.outcome is Outcome.REJECTED:
        return False
    return result.signature.key() == expected.key()
