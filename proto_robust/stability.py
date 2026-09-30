"""Repeated-trial evidence for intermittent failures.

A failure is never dismissed because one repeat happens to succeed.  Every
trial is recorded so the stability of flaky, timing-related defects stays
auditable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from proto_robust.models import FailureSignature, Outcome, TargetResult


def same_failure(result: TargetResult, expected: FailureSignature) -> bool:
    """Match the exception type together with the original defect location."""
    if result.outcome != expected.outcome:
        return False
    if expected.outcome is Outcome.CRASH:
        if result.exception_type != expected.exception_type:
            return False
    if expected.fault_file and result.fault_file != expected.fault_file:
        return False
    if expected.fault_function and result.fault_function != expected.fault_function:
        return False
    if expected.fault_line and result.fault_line != expected.fault_line:
        return False
    return True


def satisfies_key_conditions(data: bytes, expected: FailureSignature) -> bool:
    """Confirm the candidate still carries the original defect key condition."""
    for offset, value in expected.key_conditions:
        if offset >= len(data) or data[offset] != value:
            return False
    return True


@dataclass
class StabilityEvidence:
    signature: FailureSignature
    trials: list[dict] = field(default_factory=list)
    hits: int = 0

    @property
    def runs(self) -> int:
        return len(self.trials)

    @property
    def rate(self) -> float:
        return self.hits / self.runs if self.trials else 0.0

    def to_dict(self) -> dict:
        return {
            "hits": self.hits,
            "runs": self.runs,
            "reproduction_rate": self.rate,
            "trials": self.trials,
        }


def trial_record(result: TargetResult) -> dict:
    return {
        "outcome": result.outcome.value,
        "exception_type": result.exception_type,
        "fault_file": result.fault_file,
        "fault_function": result.fault_function,
        "fault_line": result.fault_line,
        "duration": result.duration,
    }


def confirm_failure(run, signature: FailureSignature, repeats: int) -> StabilityEvidence:
    evidence = StabilityEvidence(signature)
    for _ in range(max(1, repeats)):
        result = run()
        evidence.trials.append(trial_record(result))
        if same_failure(result, signature):
            evidence.hits += 1
    return evidence
