"""Failure-preserving session-sequence reduction with an auditable trail."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from proto_robust.models import FailureSignature, SessionSequence, SessionStep, TargetResult
from proto_robust.stability import confirm_failure, same_failure, satisfies_key_conditions

Predicate = Callable[[SessionSequence], TargetResult]
DEFAULT_REPEATS = 3
ACCEPT_HIT_RATIO = 0.5


@dataclass
class ReductionReport:
    minimized: SessionSequence
    attempts: int
    critical_positions: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    baseline_hits: int = 0
    baseline_runs: int = 0
    completed: bool = True

    @property
    def baseline_rate(self) -> float:
        return self.baseline_hits / self.baseline_runs if self.baseline_runs else 0.0


def reduce_case(sequence: bytes | SessionSequence, signature: FailureSignature,
                run: Predicate, repeats: int = DEFAULT_REPEATS,
                initial_attempts: int = 0, initial_steps: list[dict] | None = None,
                on_progress=None, resume: bool = False) -> ReductionReport:
    candidate = _as_sequence(sequence)
    attempts = initial_attempts
    steps: list[dict] = list(initial_steps or [])

    def keeps_failure(value: SessionSequence):
        nonlocal attempts
        attempts += max(1, repeats)
        evidence = confirm_failure(lambda: run(value), signature, repeats, value)
        accepted = evidence.hits / evidence.runs > ACCEPT_HIT_RATIO
        if signature.fault_file or signature.fault_function or signature.fault_line or signature.key_conditions:
            accepted = accepted and satisfies_key_conditions(value, signature)
        return accepted, evidence.hits, evidence.runs, evidence.trials

    def record(step_number, action, value, accepted, hits, runs_count, trials, reason, **details):
        steps.append({
            "step": step_number, "action": action, "accepted": accepted,
            "hits": hits, "runs": runs_count, "reason": reason, "trials": trials,
            "sequence": value.to_dict(), "interaction_count": value.interaction_count,
            "size": value.size, **details,
        })

    if resume:
        accepted = True
        hits = runs_count = 0
        trials = []
    else:
        accepted, hits, runs_count, trials = keeps_failure(candidate)
    baseline_hits, baseline_runs = hits, runs_count
    if not resume:
        record(0, "baseline", candidate, accepted, hits, runs_count, trials,
               "full replay from a fresh session confirms failure type, location and required state")
        if on_progress is not None:
            on_progress(candidate, attempts, steps, False)

    step_number = max((int(step.get("step", 0)) for step in steps), default=0)
    changed = True
    while changed:
        changed = False
        action_orders = (
            ("remove-interaction", range(len(candidate.steps) - 1, -1, -1)),
            ("truncate-interaction", range(len(candidate.steps))),
            ("lower-byte", range(len(candidate.steps))),
        )
        for action, interactions in action_orders:
            for interaction in interactions:
                current = candidate.steps[interaction]
                details = {}
                if action == "remove-interaction":
                    trial = SessionSequence(candidate.steps[:interaction] + candidate.steps[interaction + 1 :])
                    if not trial.steps:
                        continue
                    details = {"removed_interaction": interaction, "removed_label": current.label}
                elif action == "truncate-interaction":
                    if len(current.data) <= 6:
                        continue
                    truncated = _truncate_message(current.data)
                    if truncated == current.data or len(truncated) < 6:
                        continue
                    trial = _replace_step(candidate, interaction, SessionStep(truncated, current.label))
                    details = {"interaction": interaction}
                else:
                    lowered = _lower_message(current.data)
                    if lowered is None:
                        continue
                    new_data, offset, old_value, new_value = lowered
                    trial = _replace_step(candidate, interaction, SessionStep(new_data, current.label))
                    details = {"interaction": interaction, "offset": offset,
                               "old_value": old_value, "new_value": new_value}
                keeps, hit_count, run_count, trial_evidence = keeps_failure(trial)
                if keeps:
                    step_number += 1
                    candidate = trial
                    changed = True
                    reason = {
                        "remove-interaction": "removing this interaction still reaches the same stateful defect",
                        "truncate-interaction": "shortening this interaction preserves ordered state and defect location",
                        "lower-byte": "lower byte still triggers the same fault after the same ordered replay",
                    }[action]
                    record(step_number, action, candidate, True, hit_count, run_count,
                           trial_evidence, reason, **details)
                    if on_progress is not None:
                        on_progress(candidate, attempts, steps, False)
                    break
            if changed:
                break

    return ReductionReport(candidate, attempts, _critical_positions(candidate, signature, run),
                           steps, baseline_hits, baseline_runs)


def _as_sequence(sequence: bytes | SessionSequence) -> SessionSequence:
    return sequence if isinstance(sequence, SessionSequence) else SessionSequence.from_bytes(sequence)


def _replace_step(sequence: SessionSequence, index: int, step: SessionStep) -> SessionSequence:
    steps = list(sequence.steps)
    steps[index] = step
    return SessionSequence(tuple(steps))


def _truncate_message(data: bytes) -> bytes:
    if len(data) < 6 or bytes(data[:2]) != b"SM":
        return data
    return _repair_sm_length(data[:-1])


def _lower_message(data: bytes):
    for offset, value in enumerate(data):
        if offset in (0, 1, 2, 4, 5):
            continue
        for replacement in (0, 1, 2, 4, 255):
            if replacement < value:
                trial = _repair_sm_length(bytearray(data[:offset] + bytes([replacement]) + data[offset + 1 :]))
                return bytes(trial), offset, value, replacement
    return None


def _critical_positions(sequence: SessionSequence, signature: FailureSignature, run: Predicate) -> list[dict]:
    positions = []
    for interaction, step in enumerate(sequence.steps):
        without = SessionSequence(sequence.steps[:interaction] + sequence.steps[interaction + 1 :])
        if not without.steps or not satisfies_key_conditions(without, signature) or not same_failure(run(without), signature):
            positions.append({"type": "interaction", "interaction": interaction, "label": step.label, "required": True})
        for offset, value in enumerate(step.data):
            changed = bytearray(step.data)
            changed[offset] = 0 if value else 1
            trial = _replace_step(sequence, interaction, SessionStep(_repair_sm_length(changed), step.label))
            guarded = bool(signature.fault_file or signature.fault_function or signature.fault_line or signature.key_conditions)
            if (guarded and not satisfies_key_conditions(trial, signature)) or not same_failure(run(trial), signature):
                positions.append({"type": "byte", "interaction": interaction, "offset": offset,
                                  "value": value, "label": step.label, "required": True})
    return positions


def _repair_sm_length(data: bytes | bytearray) -> bytes:
    if len(data) < 6 or bytes(data[:2]) != b"SM":
        return bytes(data)
    payload_length = len(data) - 6
    return b"SM" + bytes([data[2], data[3]]) + payload_length.to_bytes(2, "big") + bytes(data[6:])
