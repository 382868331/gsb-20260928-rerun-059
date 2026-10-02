"""Differential reduction for two target versions replaying one sequence.

A valid comparison always contains two fresh-process replays with the same
ordered session input: one against the old target and one against the new
target.  A preparation failure is not a negative result, because the target
was never put in the session condition required for the comparison.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from proto_robust.models import FailureSignature, Outcome
from proto_robust.sequences import Interaction, ReplayResult, SessionSequence


class DifferenceKind(str, Enum):
    OLD_ONLY = "old_reproducible_new_absent"
    DIFFERENT_LOCATION = "different_location"


PREPARATION_OUTCOME = Outcome.PREPARATION_ERROR.value


@dataclass(frozen=True)
class VersionSpec:
    identity: str
    replay_name: str
    describe_name: str

    def to_dict(self) -> dict:
        return {
            "identity": self.identity,
            "replay": self.replay_name,
            "describe": self.describe_name,
        }


@dataclass(frozen=True)
class DifferentialCondition:
    kind: DifferenceKind
    old: VersionSpec
    new: VersionSpec
    old_signature: FailureSignature
    new_signature: FailureSignature | None = None

    def to_dict(self) -> dict:
        payload = {
            "kind": self.kind.value,
            "old": self.old.to_dict(),
            "new": self.new.to_dict(),
            "old_signature": _signature_dict(self.old_signature),
        }
        if self.new_signature is not None:
            payload["new_signature"] = _signature_dict(self.new_signature)
        return payload


@dataclass(frozen=True)
class AttemptPolicy:
    valid_attempts: int = 3
    stop_after_hits: int | None = None
    stop_after_rejections: int | None = None
    stop_after_timeouts: int | None = 1
    stop_after_preparation_failures: int | None = 1
    required_hit_ratio: float = 1.0

    def to_dict(self) -> dict:
        return {
            "valid_attempts": self.valid_attempts,
            "stop_after_hits": self.stop_after_hits,
            "stop_after_rejections": self.stop_after_rejections,
            "stop_after_timeouts": self.stop_after_timeouts,
            "stop_after_preparation_failures": self.stop_after_preparation_failures,
            "required_hit_ratio": self.required_hit_ratio,
        }


@dataclass
class PairAttempt:
    name: str
    mode: str
    input_order: tuple[str, str]
    old_identity: str
    new_identity: str
    old_result: dict
    new_result: dict
    disposition: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "mode": self.mode,
            "input_order": list(self.input_order),
            "old_identity": self.old_identity,
            "new_identity": self.new_identity,
            "old": self.old_result,
            "new": self.new_result,
            "disposition": self.disposition,
        }


@dataclass
class CandidateEvaluation:
    candidate: SessionSequence
    policy: AttemptPolicy
    attempts: list[PairAttempt] = field(default_factory=list)
    accepted: bool = False
    complete: bool = False

    @property
    def valid_attempts(self) -> list[PairAttempt]:
        return [item for item in self.attempts if item.disposition != "preparation_failed"]

    def counts(self) -> dict[str, int]:
        counts = {"hit": 0, "rejected": 0, "timeout": 0, "preparation_failed": 0}
        for attempt in self.attempts:
            counts[attempt.disposition] = counts.get(attempt.disposition, 0) + 1
        return counts

    def to_dict(self) -> dict:
        counts = self.counts()
        return {
            "candidate": self.candidate.to_dict(),
            "policy": self.policy.to_dict(),
            "accepted": self.accepted,
            "complete": self.complete,
            "counts": counts,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
        }

    @classmethod
    def from_dict(cls, payload: dict, candidate: SessionSequence | None = None) -> "CandidateEvaluation":
        policy_payload = payload.get("policy", {})
        evaluation = cls(
            candidate if candidate is not None else SessionSequence.from_dict(payload["candidate"]),
            AttemptPolicy(
                valid_attempts=int(policy_payload.get("valid_attempts", 3)),
                stop_after_hits=policy_payload.get("stop_after_hits"),
                stop_after_rejections=policy_payload.get("stop_after_rejections"),
                stop_after_timeouts=policy_payload.get("stop_after_timeouts", 1),
                stop_after_preparation_failures=policy_payload.get("stop_after_preparation_failures", 1),
                required_hit_ratio=float(policy_payload.get("required_hit_ratio", 1.0)),
            ),
            attempts=[PairAttempt(
                name=str(item["name"]),
                mode=str(item.get("mode", "group")),
                input_order=tuple(item.get("input_order", [])),
                old_identity=str(item.get("old_identity", "")),
                new_identity=str(item.get("new_identity", "")),
                old_result=dict(item.get("old", {})),
                new_result=dict(item.get("new", {})),
                disposition=str(item["disposition"]),
            ) for item in payload.get("attempts", [])],
            accepted=bool(payload.get("accepted", False)),
            complete=bool(payload.get("complete", False)),
        )
        return evaluation


def result_matches(result: ReplayResult | dict, signature: FailureSignature) -> bool:
    payload = result.to_dict() if isinstance(result, ReplayResult) else dict(result)
    if payload.get("outcome") != signature.outcome.value:
        return False
    if signature.exception_type and payload.get("exception_type") != signature.exception_type:
        return False
    if signature.fault_file and payload.get("fault_file") != signature.fault_file:
        return False
    if signature.fault_function and payload.get("fault_function") != signature.fault_function:
        return False
    if signature.fault_line and payload.get("fault_line") != signature.fault_line:
        return False
    if signature.state_path and payload.get("state_path") != signature.state_path:
        return False
    observed = {
        (item.get("offset"), item.get("value"))
        for item in payload.get("key_conditions", [])
    }
    expected = {(offset, value) for _step, offset, value in signature.step_key_conditions}
    return expected.issubset(observed)


def classify_pair(old_result: ReplayResult | dict, new_result: ReplayResult | dict,
                  condition: DifferentialCondition) -> str:
    old = old_result.to_dict() if isinstance(old_result, ReplayResult) else old_result
    new = new_result.to_dict() if isinstance(new_result, ReplayResult) else new_result
    if old.get("outcome") == PREPARATION_OUTCOME or new.get("outcome") == PREPARATION_OUTCOME:
        return "preparation_failed"
    if old.get("outcome") == Outcome.TIMEOUT.value or new.get("outcome") == Outcome.TIMEOUT.value:
        return "timeout"
    if not result_matches(old, condition.old_signature):
        return "rejected"
    if condition.kind is DifferenceKind.OLD_ONLY:
        if new.get("outcome") in {Outcome.CRASH.value, Outcome.RUNNER_ERROR.value, Outcome.TIMEOUT.value}:
            return "rejected"
        return "hit"
    if condition.kind is DifferenceKind.DIFFERENT_LOCATION:
        expected_new = condition.new_signature
        if expected_new is None:
            return "rejected"
        if not result_matches(new, expected_new):
            return "rejected"
        same_location = (
            old.get("fault_file") == new.get("fault_file")
            and old.get("fault_function") == new.get("fault_function")
            and old.get("fault_line") == new.get("fault_line")
        )
        return "rejected" if same_location else "hit"
    return "rejected"

RunPair = Callable[[SessionSequence, str, str], tuple[ReplayResult, ReplayResult]]


def evaluate_candidate(
    candidate: SessionSequence,
    condition: DifferentialCondition,
    run_pair: RunPair,
    policy: AttemptPolicy | None = None,
    name_prefix: str = "candidate",
    mode: str = "solo",
    evaluation: CandidateEvaluation | None = None,
    can_attempt: Callable[[], bool] | None = None,
    attempt_budget: int | None = None,
    attempts_before: int = 0,
) -> CandidateEvaluation:
    policy = policy or AttemptPolicy()
    if evaluation is None:
        evaluation = CandidateEvaluation(candidate, policy)
    while not evaluation.complete:
        if can_attempt is not None and not can_attempt():
            break
        if attempt_budget is not None and attempts_before + len(evaluation.attempts) >= attempt_budget:
            break
        counts = evaluation.counts()
        valid = len(evaluation.valid_attempts)
        if valid >= policy.valid_attempts:
            break
        if policy.stop_after_hits is not None and counts["hit"] >= policy.stop_after_hits:
            break
        if policy.stop_after_rejections is not None and counts["rejected"] >= policy.stop_after_rejections:
            break
        if policy.stop_after_timeouts is not None and counts["timeout"] >= policy.stop_after_timeouts:
            break
        if policy.stop_after_preparation_failures is not None and counts["preparation_failed"] >= policy.stop_after_preparation_failures:
            break
        attempt_name = f"{name_prefix}-pair-{len(evaluation.attempts)}"
        order = (condition.old.identity, condition.new.identity)
        old_result, new_result = run_pair(candidate, attempt_name, mode)
        disposition = classify_pair(old_result, new_result, condition)
        evaluation.attempts.append(PairAttempt(
            name=attempt_name,
            mode=mode,
            input_order=order,
            old_identity=condition.old.identity,
            new_identity=condition.new.identity,
            old_result=old_result.to_dict(),
            new_result=new_result.to_dict(),
            disposition=disposition,
        ))
    counts = evaluation.counts()
    valid = len(evaluation.valid_attempts)
    evaluation.complete = (
        valid >= policy.valid_attempts
        or (policy.stop_after_hits is not None and counts["hit"] >= policy.stop_after_hits)
        or (policy.stop_after_rejections is not None and counts["rejected"] >= policy.stop_after_rejections)
        or (policy.stop_after_timeouts is not None and counts["timeout"] >= policy.stop_after_timeouts)
        or (policy.stop_after_preparation_failures is not None
            and counts["preparation_failed"] >= policy.stop_after_preparation_failures)
    )
    if valid:
        evaluation.accepted = counts["hit"] / valid >= policy.required_hit_ratio
    return evaluation


@dataclass
class DifferentialReductionReport:
    original: SessionSequence
    minimized: SessionSequence
    condition: DifferentialCondition
    attempts: int = 0
    complete: bool = False
    accepted: bool = False
    steps: list[dict] = field(default_factory=list)

    def to_checkpoint(self) -> dict:
        return {
            "original": self.original.to_dict(),
            "minimized": self.minimized.to_dict(),
            "condition": self.condition.to_dict(),
            "attempts": self.attempts,
            "complete": self.complete,
            "accepted": self.accepted,
            "steps": self.steps,
        }

    @classmethod
    def from_checkpoint(cls, payload: dict) -> "DifferentialReductionReport":
        condition_payload = payload["condition"]
        condition = DifferentialCondition(
            DifferenceKind(condition_payload["kind"]),
            _version_from_dict(condition_payload["old"]),
            _version_from_dict(condition_payload["new"]),
            _signature_from_dict(condition_payload["old_signature"]),
            _signature_from_dict(condition_payload.get("new_signature"))
            if condition_payload.get("new_signature") else None,
        )
        return cls(
            original=SessionSequence.from_dict(payload["original"]),
            minimized=SessionSequence.from_dict(payload["minimized"]),
            condition=condition,
            attempts=int(payload.get("attempts", 0)),
            complete=bool(payload.get("complete", False)),
            accepted=bool(payload.get("accepted", False)),
            steps=list(payload.get("steps", [])),
        )


def reduce_differential_sequence(
    sequence: SessionSequence,
    condition: DifferentialCondition,
    run_pair: RunPair,
    policy: AttemptPolicy | None = None,
    report: DifferentialReductionReport | None = None,
    max_pair_attempts: int | None = None,
    pair_attempts_used: int = 0,
) -> tuple[DifferentialReductionReport, int, bool]:
    policy = policy or AttemptPolicy()
    attempted_sequences = {
        tuple(frame["hex"] for frame in step.get("candidate", {}).get("steps", []))
        for step in (report.steps if report is not None else [])
    }
    if report is None:
        baseline = evaluate_candidate(
            sequence, condition, run_pair, policy, "baseline", "solo",
            can_attempt=lambda: max_pair_attempts is None or pair_attempts_used < max_pair_attempts,
            attempt_budget=max_pair_attempts,
            attempts_before=pair_attempts_used,
        )
        pair_attempts_used += len(baseline.attempts)
        report = DifferentialReductionReport(
            original=sequence,
            minimized=sequence,
            condition=condition,
            attempts=len(baseline.attempts),
            complete=False,
            steps=[{"step": 0, "action": "baseline", **baseline.to_dict()}],
        )
        if not baseline.accepted:
            report.complete = baseline.complete
            return report, pair_attempts_used, max_pair_attempts is not None and pair_attempts_used >= max_pair_attempts
        report.accepted = True
    if max_pair_attempts is not None and pair_attempts_used >= max_pair_attempts:
        return report, pair_attempts_used, True

    while True:
        current = report.minimized
        progressed = False
        for position in range(len(current.steps) - 1, -1, -1):
            if max_pair_attempts is not None and pair_attempts_used >= max_pair_attempts:
                return report, pair_attempts_used, True
            if position == 0:
                continue
            steps = list(current.steps[:position]) + list(current.steps[position + 1:])
            candidate = _reindex(current, steps)
            if _sequence_key(candidate) in attempted_sequences:
                continue
            evaluation = evaluate_candidate(
                candidate, condition, run_pair, policy, f"drop-{position}", "group",
                can_attempt=lambda used=pair_attempts_used: max_pair_attempts is None or used < max_pair_attempts,
                attempt_budget=max_pair_attempts,
                attempts_before=pair_attempts_used,
            )
            pair_attempts_used += len(evaluation.attempts)
            report.attempts += len(evaluation.attempts)
            report.steps.append({
                "step": len(report.steps),
                "action": "drop-interaction",
                "position": position,
                **evaluation.to_dict(),
            })
            attempted_sequences.add(_sequence_key(candidate))
            if not evaluation.complete:
                return report, pair_attempts_used, True
            if evaluation.accepted:
                report.minimized = candidate
                report.accepted = True
                progressed = True
                break
            if not evaluation.complete:
                return report, pair_attempts_used, True
        if progressed:
            continue

        for step_index, interaction in enumerate(current.steps):
            changed = False
            data = bytearray(interaction.data)
            for offset in range(1, len(data)):
                for replacement in (0, 1, 2, 4, 255):
                    if replacement >= data[offset]:
                        continue
                    trial_data = bytearray(data)
                    trial_data[offset] = replacement
                    steps = list(current.steps)
                    steps[step_index] = Interaction(interaction.name, bytes(trial_data))
                    candidate = _reindex(current, steps)
                    if _sequence_key(candidate) in attempted_sequences:
                        continue
                    evaluation = evaluate_candidate(
                        candidate, condition, run_pair, policy,
                        f"byte-{step_index}-{offset}", "group",
                        can_attempt=lambda used=pair_attempts_used: max_pair_attempts is None or used < max_pair_attempts,
                        attempt_budget=max_pair_attempts,
                        attempts_before=pair_attempts_used,
                    )
                    pair_attempts_used += len(evaluation.attempts)
                    report.attempts += len(evaluation.attempts)
                    report.steps.append({
                        "step": len(report.steps),
                        "action": "lower-byte",
                        "position": step_index,
                        "offset": offset,
                        "new_value": replacement,
                        **evaluation.to_dict(),
                    })
                    attempted_sequences.add(_sequence_key(candidate))
                    if not evaluation.complete:
                        return report, pair_attempts_used, True
                    if evaluation.accepted:
                        report.minimized = candidate
                        report.accepted = True
                        changed = True
                        progressed = True
                        break
                    if not evaluation.complete:
                        return report
                if changed:
                    break
            if changed:
                break

        if not progressed:
            report.complete = True
            return report, pair_attempts_used, False


def _reindex(original: SessionSequence, steps: list[Interaction]) -> SessionSequence:
    return SessionSequence(original.index, original.label, original.start_state, tuple(steps))


def _sequence_key(sequence: SessionSequence) -> tuple[str, ...]:
    return tuple(step.data.hex() for step in sequence.steps)


def _signature_dict(signature: FailureSignature) -> dict:
    return {
        "outcome": signature.outcome.value,
        "exception_type": signature.exception_type,
        "fault_file": signature.fault_file,
        "fault_function": signature.fault_function,
        "fault_line": signature.fault_line,
        "state_path": signature.state_path,
        "step_key_conditions": [
            {"step": step, "offset": offset, "value": value}
            for step, offset, value in signature.step_key_conditions
        ],
        "key": signature.key(),
    }


def _version_from_dict(payload: dict) -> VersionSpec:
    return VersionSpec(
        identity=str(payload["identity"]),
        replay_name=str(payload["replay"]),
        describe_name=str(payload["describe"]),
    )


def _signature_from_dict(payload: dict | None) -> FailureSignature | None:
    if payload is None:
        return None
    return FailureSignature(
        Outcome(payload["outcome"]),
        exception_type=payload.get("exception_type", ""),
        message=payload.get("message", ""),
        fault_file=payload.get("fault_file", ""),
        fault_function=payload.get("fault_function", ""),
        fault_line=int(payload.get("fault_line", 0)),
        state_path=payload.get("state_path", ""),
        step_key_conditions=tuple(
            (int(item["step"]), int(item["offset"]), int(item["value"]))
            for item in payload.get("step_key_conditions", [])
        ),
    )
