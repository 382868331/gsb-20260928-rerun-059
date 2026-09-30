"""Failure-preserving input reduction with an auditable step trail.

Every accepted step must still trigger the *same* defect, identified by both
the exception type and the original defect location.  Intermittent defects are
repeated; a step is accepted on majority reproduction, never deleted because a
single trial missed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from proto_robust.models import FailureSignature, TargetResult
from proto_robust.stability import confirm_failure, same_failure, satisfies_key_conditions

Predicate = Callable[[bytes], TargetResult]

# Odd number avoids a tie; 3 trials are cheap yet expose flakiness.
DEFAULT_REPEATS = 3
ACCEPT_HIT_RATIO = 0.5


@dataclass
class ReductionReport:
    minimized: bytes
    attempts: int
    critical_byte_offsets: list[int] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    baseline_hits: int = 0
    baseline_runs: int = 0

    @property
    def baseline_rate(self) -> float:
        return self.baseline_hits / self.baseline_runs if self.baseline_runs else 0.0


def reduce_case(
    data: bytes,
    signature: FailureSignature,
    run: Predicate,
    repeats: int = DEFAULT_REPEATS,
) -> ReductionReport:
    candidate = bytearray(data)
    attempts = 0
    steps: list[dict] = []

    def keeps_failure(value: bytes | bytearray) -> tuple[bool, int, int, list[dict]]:
        nonlocal attempts
        attempts += max(1, repeats)
        evidence = confirm_failure(lambda: run(bytes(value)), signature, repeats)
        reproduced = evidence.hits / evidence.runs > ACCEPT_HIT_RATIO
        accepted = reproduced and satisfies_key_conditions(bytes(value), signature)
        return accepted, evidence.hits, evidence.runs, evidence.trials

    accepted, hits, runs_count, trials = keeps_failure(candidate)
    baseline_hits, baseline_runs = hits, runs_count
    steps.append({
        "step": 0,
        "action": "baseline",
        "hex": bytes(candidate).hex(),
        "size": len(candidate),
        "accepted": accepted,
        "hits": hits,
        "runs": runs_count,
        "reason": "original sample re-run to confirm defect type, location and key conditions",
        "trials": trials,
    })

    step_number = 0
    changed = True
    while changed:
        changed = False
        for position in range(len(candidate) - 1, 0, -1):
            trial = _repair_sm_length(candidate[:position])
            if len(trial) < 6:
                continue
            keeps, hits, runs_count, trials = keeps_failure(trial)
            if keeps:
                step_number += 1
                candidate = bytearray(trial)
                changed = True
                steps.append({
                    "step": step_number,
                    "action": "truncate",
                    "position": position,
                    "hex": bytes(candidate).hex(),
                    "size": len(candidate),
                    "accepted": True,
                    "hits": hits,
                    "runs": runs_count,
                    "reason": f"prefix of length {position} still raises the same exception at the same defect location",
                    "trials": trials,
                })
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
                keeps, hits, runs_count, trials = keeps_failure(trial)
                if keeps:
                    step_number += 1
                    candidate = trial
                    changed = True
                    steps.append({
                        "step": step_number,
                        "action": "lower-byte",
                        "position": position,
                        "old_value": value,
                        "new_value": replacement,
                        "hex": bytes(candidate).hex(),
                        "size": len(candidate),
                        "accepted": True,
                        "hits": hits,
                        "runs": runs_count,
                        "reason": (
                            f"byte at offset {position} lowered to {replacement}; "
                            "same exception still raised at the same defect location and key condition"
                        ),
                        "trials": trials,
                    })
                    break
            if changed:
                break

    minimized = bytes(candidate)
    critical = _critical_offsets(minimized, signature, run)
    return ReductionReport(minimized, attempts, critical, steps, baseline_hits, baseline_runs)


def _critical_offsets(data: bytes, signature: FailureSignature, run: Predicate) -> list[int]:
    offsets: list[int] = []
    for position in range(len(data)):
        trial = _repair_sm_length(bytearray(data[:position] + b"\x00" + data[position + 1 :]))
        if not satisfies_key_conditions(bytes(trial), signature) or not same_failure(run(bytes(trial)), signature):
            offsets.append(position)
    return offsets


def _repair_sm_length(data: bytes | bytearray) -> bytes:
    if len(data) < 6 or bytes(data[:2]) != b"SM":
        return bytes(data)
    payload_length = len(data) - 6
    return b"SM" + bytes([data[2], data[3]]) + payload_length.to_bytes(2, "big") + bytes(data[6:])
