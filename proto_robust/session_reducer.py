"""State-preserving reduction of multi-step session sequences.

Reduction shrinks along two axes:

* interaction prefix / non-essential steps are removed while preserving the
  complete session starting point and ordered state build-up that the defect
  requires;
* individual frame bytes are simplified.

Every accepted candidate is replayed from a clean session and must still hit
the same defect: same outcome, same defect location *and* the same session
state path (state/mode/bucket).  This prevents a state-gated commit defect from
being reduced into the separate mode-table IndexError.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from proto_robust.models import FailureSignature, Outcome
from proto_robust.sequences import Interaction, ReplayResult, SessionSequence

ACCEPT_HIT_RATIO = 0.5


def same_failure(result: ReplayResult, signature: FailureSignature) -> bool:
    if result.outcome != signature.outcome.value:
        return False
    if signature.outcome is Outcome.CRASH:
        if result.exception_type != signature.exception_type:
            return False
    if signature.fault_file and result.fault_file != signature.fault_file:
        return False
    if signature.fault_function and result.fault_function != signature.fault_function:
        return False
    if signature.fault_line and result.fault_line != signature.fault_line:
        return False
    if signature.state_path and result.state_path != signature.state_path:
        return False
    if not satisfies_key_conditions(result, signature):
        return False
    return True


def satisfies_key_conditions(result: ReplayResult, signature: FailureSignature) -> bool:
    # Command/key bytes identify the defect-triggering interaction by value,
    # not by its position, so dropping prefix interactions cannot desync them.
    expected = {(offset, value) for _step, offset, value in signature.step_key_conditions}
    observed = {(offset, value) for _step, offset, value in result.key_conditions}
    return expected.issubset(observed)


@dataclass
class SequenceReductionReport:
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
    def from_checkpoint(cls, payload: dict) -> "SequenceReductionReport":
        return cls(
            minimized=SessionSequence.from_dict(payload["minimized"]),
            attempts=int(payload.get("attempts", 0)),
            steps=list(payload.get("steps", [])),
            baseline_hits=int(payload.get("baseline_hits", 0)),
            baseline_runs=int(payload.get("baseline_runs", 0)),
            complete=bool(payload.get("complete", False)),
        )


def _trial_summary(trials: list[ReplayResult], signature: FailureSignature) -> tuple[int, int, list[dict]]:
    hits = sum(1 for trial in trials if same_failure(trial, signature))
    return hits, len(trials), [trial.to_dict() for trial in trials]


def reduce_sequence(
    sequence: SessionSequence,
    signature: FailureSignature,
    run,
    repeats: int = 3,
    budget: int | None = None,
    spent: int = 0,
    report: SequenceReductionReport | None = None,
) -> tuple[SequenceReductionReport, int, bool]:
    """Reduce while metering replays against ``budget``.

    Returns (report, total replays consumed, exhausted).  When the budget is
    exhausted mid-reduction the current best candidate is preserved and the
    reduction can be continued on a later resumed run.
    """

    def evaluate(candidate: SessionSequence) -> tuple[bool, int, int, list[dict], int]:
        nonlocal spent
        trials: list[ReplayResult] = []
        for _ in range(max(1, repeats)):
            if budget is not None and spent >= budget:
                break
            trials.append(run(candidate))
            spent += 1
        hits, runs_count, evidence = _trial_summary(trials, signature)
        accepted = bool(runs_count) and hits / runs_count > ACCEPT_HIT_RATIO
        return accepted, hits, runs_count, evidence, spent

    if report is None:
        current = sequence
        accepted, hits, runs_count, evidence, spent = evaluate(sequence)
        report = SequenceReductionReport(
            minimized=sequence,
            attempts=runs_count,
            baseline_hits=hits,
            baseline_runs=runs_count,
        )
        report.steps.append({
            "step": 0,
            "action": "baseline",
            "hits": hits,
            "runs": runs_count,
            "interactions": len(sequence.steps),
            "sequence_hex": [step.data.hex() for step in sequence.steps],
            "accepted": accepted,
            "reason": "original sequence replayed from clean session start; state path and defect location confirmed",
            "trials": evidence,
        })
        if accepted:
            report.minimized = sequence

    exhausted = budget is not None and spent >= budget
    step_number = len(report.steps)

    while not exhausted:
        progressed = False
        current = report.minimized

        # Axis 1: drop one non-failing, non-opening interaction at a time.
        failing_step = current_failing_step(report.minimized, run)
        for position in range(len(current.steps) - 1, -1, -1):
            if position == 0:
                # The session start OPEN anchors the clean session start point.
                continue
            if position == failing_step:
                continue
            candidate_steps = list(current.steps[:position]) + list(current.steps[position + 1 :])
            candidate = _reindex(current, candidate_steps)
            accepted, hits, runs_count, evidence, spent = evaluate(candidate)
            if accepted:
                step_number += 1
                report.minimized = candidate
                report.attempts += runs_count
                report.steps.append({
                    "step": step_number,
                    "action": "drop-interaction",
                    "position": position,
                    "dropped": current.steps[position].name,
                    "interactions": len(candidate.steps),
                    "sequence_hex": [step.data.hex() for step in candidate.steps],
                    "accepted": True,
                    "hits": hits,
                    "runs": runs_count,
                    "reason": (
                        f"interaction {position} ({current.steps[position].name}) removed; remaining prefix "
                        "still builds the required state and the same defect fires at the same location"
                    ),
                    "trials": evidence,
                })
                progressed = True
                break
            report.attempts += runs_count
            if budget is not None and spent >= budget:
                exhausted = True
                break
        if progressed or exhausted:
            continue

        # Axis 2: simplify payload/key bytes while keeping command and state.
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
                    accepted, hits, runs_count, evidence, spent = evaluate(candidate)
                    if accepted:
                        step_number += 1
                        report.minimized = candidate
                        report.attempts += runs_count
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
                            "hits": hits,
                            "runs": runs_count,
                            "reason": (
                                f"byte at interaction {step_index} offset {offset} lowered to {replacement}; "
                                "required state path and defect location are unchanged"
                            ),
                            "trials": evidence,
                        })
                        changed_byte = True
                        break
                    report.attempts += runs_count
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


def current_failing_step(sequence: SessionSequence, run) -> int:
    result = run(sequence)
    return result.failing_step if result.failing_step >= 0 else len(sequence.steps) - 1


def _reindex(original: SessionSequence, steps: list[Interaction]) -> SessionSequence:
    return SessionSequence(original.index, original.label, original.start_state, tuple(steps))
