"""Difference-preserving reduction for two-version comparison candidates.

A comparison candidate is only reduced while the *researcher-selected
version-difference condition* still holds, not merely while one build keeps
raising anything:

* the failing side must still reproduce its original failure signature
  (outcome, defect location, session state path, key bytes); and
* the other side must still satisfy the selected condition -- either it
  observes no failure at all (not_observed) or it fails with its own
  distinct signature (different_location).

Every candidate is replayed from a clean session start on *both* builds, so a
reduction that collapses the difference (e.g. the new build suddenly hits the
same defect, or the old build stops failing) is rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from proto_robust.models import FailureSignature, Outcome
from proto_robust.session_reducer import same_failure
from proto_robust.sequences import Interaction, ReplayResult, SessionSequence

ACCEPT_HIT_RATIO = 0.5
FAILURE_OUTCOMES = {Outcome.CRASH.value, Outcome.TIMEOUT.value, Outcome.RUNNER_ERROR.value}


def condition_holds(result: ReplayResult, condition: dict, other_signature: FailureSignature | None) -> bool:
    """Check the other build still satisfies the version-difference condition."""
    kind = condition.get("kind", "not_observed")
    if kind == "not_observed":
        return result.outcome not in FAILURE_OUTCOMES
    if kind == "different_location":
        if result.outcome not in FAILURE_OUTCOMES:
            return False
        return other_signature is not None and same_failure(result, other_signature)
    raise ValueError(f"unknown difference condition {kind!r}")


@dataclass
class DifferenceReductionReport:
    minimized: SessionSequence
    attempts: int = 0
    steps: list[dict] = field(default_factory=list)
    baseline_hits: int = 0
    baseline_runs: int = 0
    complete: bool = False

    def to_checkpoint(self) -> dict:
        return {
            "minimized": self.minimized.to_dict(),
            "attempts": self.attempts,
            "steps": self.steps,
            "baseline_hits": self.baseline_hits,
            "baseline_runs": self.baseline_runs,
            "complete": self.complete,
        }

    @classmethod
    def from_checkpoint(cls, payload: dict) -> "DifferenceReductionReport":
        return cls(
            minimized=SessionSequence.from_dict(payload["minimized"]),
            attempts=int(payload.get("attempts", 0)),
            steps=list(payload.get("steps", [])),
            baseline_hits=int(payload.get("baseline_hits", 0)),
            baseline_runs=int(payload.get("baseline_runs", 0)),
            complete=bool(payload.get("complete", False)),
        )


def reduce_difference(
    sequence: SessionSequence,
    failing_signature: FailureSignature,
    condition: dict,
    run_failing,
    run_other,
    other_signature: FailureSignature | None = None,
    repeats: int = 2,
    budget: int | None = None,
    spent: int = 0,
    report: DifferenceReductionReport | None = None,
) -> tuple[DifferenceReductionReport, int, bool]:
    """Reduce while metering replays on both builds against budget.

    Returns (report, total replays consumed, exhausted).  Each candidate
    evaluation replays on the failing side *and* the other side, so one
    candidate costs 2 * repeats replays.
    """

    def evaluate(candidate: SessionSequence) -> tuple[bool, dict, int]:
        nonlocal spent
        failing_trials: list[ReplayResult] = []
        other_trials: list[ReplayResult] = []
        for _ in range(max(1, repeats)):
            if budget is not None and spent >= budget:
                break
            failing_trials.append(run_failing(candidate))
            spent += 1
            if budget is not None and spent >= budget:
                break
            other_trials.append(run_other(candidate))
            spent += 1
        failing_hits = sum(1 for trial in failing_trials if same_failure(trial, failing_signature))
        failing_ok = bool(failing_trials) and failing_hits / len(failing_trials) > ACCEPT_HIT_RATIO
        other_ok_count = sum(
            1 for trial in other_trials if condition_holds(trial, condition, other_signature)
        )
        other_ok = bool(other_trials) and other_ok_count / len(other_trials) > ACCEPT_HIT_RATIO
        evidence = {
            "failing": {
                "hits": failing_hits,
                "runs": len(failing_trials),
                "trials": [trial.to_dict() for trial in failing_trials],
            },
            "other": {
                "condition_holds": other_ok_count,
                "runs": len(other_trials),
                "trials": [trial.to_dict() for trial in other_trials],
            },
        }
        return failing_ok and other_ok, evidence, spent

    if report is None:
        accepted, evidence, spent = evaluate(sequence)
        report = DifferenceReductionReport(
            minimized=sequence,
            baseline_hits=evidence["failing"]["hits"],
            baseline_runs=evidence["failing"]["runs"],
        )
        report.steps.append({
            "step": 0,
            "action": "baseline",
            "interactions": len(sequence.steps),
            "sequence_hex": [step.data.hex() for step in sequence.steps],
            "accepted": accepted,
            "reason": (
                "original sequence replayed on both builds from clean session starts; "
                "failing side reproduces its signature and the other side still satisfies "
                "the version-difference condition"
            ),
            "evidence": evidence,
        })
        if accepted:
            report.minimized = sequence

    exhausted = budget is not None and spent >= budget
    step_number = len(report.steps)

    while not exhausted:
        progressed = False
        current = report.minimized

        # Axis 1: drop one non-essential interaction (never the session start).
        for position in range(len(current.steps) - 1, 0, -1):
            candidate_steps = list(current.steps[:position]) + list(current.steps[position + 1 :])
            candidate = _reindex(current, candidate_steps)
            accepted, evidence, spent = evaluate(candidate)
            report.attempts += evidence["failing"]["runs"] + evidence["other"]["runs"]
            if accepted:
                step_number += 1
                report.minimized = candidate
                report.steps.append({
                    "step": step_number,
                    "action": "drop-interaction",
                    "position": position,
                    "dropped": current.steps[position].name,
                    "interactions": len(candidate.steps),
                    "sequence_hex": [step.data.hex() for step in candidate.steps],
                    "accepted": True,
                    "reason": (
                        f"interaction {position} ({current.steps[position].name}) removed; failing side "
                        "still reproduces the same defect and the other side still satisfies the "
                        "version-difference condition"
                    ),
                    "evidence": evidence,
                })
                progressed = True
                break
            if budget is not None and spent >= budget:
                exhausted = True
                break
        if progressed or exhausted:
            continue

        # Axis 2: lower key/payload bytes while both sides keep the difference.
        for step_index, interaction in enumerate(current.steps):
            data = bytearray(interaction.data)
            if len(data) < 3:
                continue
            changed_byte = False
            for offset in range(1, len(data)):
                original_value = data[offset]
                for replacement in (0, 1, 2, 4, 255):
                    if replacement >= original_value:
                        continue
                    trial = bytearray(data)
                    trial[offset] = replacement
                    candidate_steps = list(current.steps)
                    candidate_steps[step_index] = Interaction(interaction.name, bytes(trial))
                    candidate = _reindex(current, candidate_steps)
                    accepted, evidence, spent = evaluate(candidate)
                    report.attempts += evidence["failing"]["runs"] + evidence["other"]["runs"]
                    if accepted:
                        step_number += 1
                        report.minimized = candidate
                        report.steps.append({
                            "step": step_number,
                            "action": "lower-byte",
                            "position": step_index,
                            "offset": offset,
                            "old_value": original_value,
                            "new_value": replacement,
                            "interactions": len(candidate.steps),
                            "sequence_hex": [step.data.hex() for step in candidate.steps],
                            "accepted": True,
                            "reason": (
                                f"byte at interaction {step_index} offset {offset} lowered to "
                                f"{replacement}; the version difference is preserved on both builds"
                            ),
                            "evidence": evidence,
                        })
                        changed_byte = True
                        break
                    if budget is not None and spent >= budget:
                        exhausted = True
                        break
                if changed_byte or exhausted:
                    break
            if changed_byte:
                progressed = True
                break

        if not progressed:
            report.complete = True
            break

    return report, spent, exhausted


def _reindex(original: SessionSequence, steps: list[Interaction]) -> SessionSequence:
    return SessionSequence(original.index, original.label, original.start_state, tuple(steps))
