"""Paired differential trials for two target versions replaying one sequence.

A valid comparison always contains two fresh-process replays with the same
ordered session input: one against the old target and one against the new
target.  Because the version that runs first can change the starting
conditions of the version that runs second, every pair records the actual
execution order, both sides' real session start points, their preparation
results and the exact inputs that were replayed.  Rounds can follow a fixed
order or alternate directions so researchers can tell whether a difference
holds under both orders or only under a specific precondition.

A preparation failure is not a negative result, because that side was never
put in the session condition required for the comparison.  A round where one
side never ran at all is "incomplete" and is listed separately from valid
trials, timeouts and preparation failures.  The two sides of one pair are
recorded atomically: evidence is never spliced across pairs, and solo reruns
keep their own identity instead of overwriting paired results.
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
    stop_after_incomplete: int | None = 1
    required_hit_ratio: float = 1.0
    require_both_orders: bool = False

    def to_dict(self) -> dict:
        return {
            "valid_attempts": self.valid_attempts,
            "stop_after_hits": self.stop_after_hits,
            "stop_after_rejections": self.stop_after_rejections,
            "stop_after_timeouts": self.stop_after_timeouts,
            "stop_after_preparation_failures": self.stop_after_preparation_failures,
            "stop_after_incomplete": self.stop_after_incomplete,
            "required_hit_ratio": self.required_hit_ratio,
            "require_both_orders": self.require_both_orders,
        }


DISPOSITIONS = ("hit", "rejected", "timeout", "preparation_failed", "incomplete")


@dataclass
class PairAttempt:
    name: str
    mode: str
    round_kind: str
    input_order: tuple[str, str]
    inputs: tuple[str, ...]
    old_identity: str
    new_identity: str
    old_result: dict
    new_result: dict
    disposition: str

    @property
    def old_ran(self) -> bool:
        return bool(self.old_result)

    @property
    def new_ran(self) -> bool:
        return bool(self.new_result)

    @staticmethod
    def _prepared(result: dict) -> bool:
        return bool(result) and result.get("outcome") != PREPARATION_OUTCOME

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "mode": self.mode,
            "round_kind": self.round_kind,
            "input_order": list(self.input_order),
            "inputs": list(self.inputs),
            "old_identity": self.old_identity,
            "new_identity": self.new_identity,
            "old_start_state": self.old_result.get("start_state", ""),
            "new_start_state": self.new_result.get("start_state", ""),
            "old_prepared": self._prepared(self.old_result),
            "new_prepared": self._prepared(self.new_result),
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
        return [
            item for item in self.attempts
            if item.disposition not in ("preparation_failed", "incomplete")
        ]

    def counts(self) -> dict[str, int]:
        counts = {name: 0 for name in DISPOSITIONS}
        for attempt in self.attempts:
            counts[attempt.disposition] = counts.get(attempt.disposition, 0) + 1
        return counts

    def order_conclusion(self) -> dict:
        """Explain whether the difference holds under every execution order.

        Only the two sides of the same pair are ever compared; pairs are
        grouped by their recorded execution direction so a hit observed in
        one order is never combined with the opposite side of another pair.
        """
        per_order: dict[str, dict[str, int]] = {}
        for attempt in self.attempts:
            direction = "->".join(attempt.input_order)
            stats = per_order.setdefault(direction, {name: 0 for name in DISPOSITIONS} | {"valid": 0})
            stats[attempt.disposition] = stats.get(attempt.disposition, 0) + 1
            if attempt.disposition not in ("preparation_failed", "incomplete"):
                stats["valid"] += 1
        valid_directions = [d for d, stats in per_order.items() if stats["valid"] > 0]
        hit_directions = [d for d in valid_directions if per_order[d]["hit"] > 0]
        missed_directions = [d for d in valid_directions if per_order[d]["hit"] == 0]
        if not valid_directions:
            conclusion = "no_valid_rounds"
        elif len(valid_directions) > 1 and not missed_directions:
            conclusion = "both_orders"
        elif missed_directions and hit_directions:
            conclusion = "order_conditional"
        elif hit_directions:
            conclusion = "single_order_only"
        else:
            conclusion = "no_valid_difference"
        order_limited = conclusion != "both_orders"
        if conclusion == "both_orders":
            explanation = "difference reproduces under every recorded execution order"
        elif conclusion == "order_conditional":
            first = hit_directions[0].split("->")[0]
            explanation = (
                f"difference confirmed only when '{first}' runs first; "
                f"rounds ordered {missed_directions[0]} did not reproduce it"
            )
        elif conclusion == "single_order_only":
            explanation = (
                "difference confirmed only under the single recorded order "
                f"{hit_directions[0]}; no valid round in any other order"
            )
        elif conclusion == "no_valid_difference":
            explanation = "valid rounds exist but none reproduced the selected difference"
        else:
            explanation = "no valid rounds; every round was a preparation failure or incomplete"
        return {
            "conclusion": conclusion,
            "order_limited": order_limited,
            "per_order": per_order,
            "explanation": explanation,
        }

    def to_dict(self) -> dict:
        counts = self.counts()
        return {
            "candidate": self.candidate.to_dict(),
            "policy": self.policy.to_dict(),
            "accepted": self.accepted,
            "complete": self.complete,
            "counts": counts,
            "order_conclusion": self.order_conclusion(),
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
                stop_after_incomplete=policy_payload.get("stop_after_incomplete", 1),
                required_hit_ratio=float(policy_payload.get("required_hit_ratio", 1.0)),
                require_both_orders=bool(policy_payload.get("require_both_orders", False)),
            ),
            attempts=[PairAttempt(
                name=str(item["name"]),
                mode=str(item.get("mode", "group")),
                round_kind=str(item.get("round_kind", "fixed")),
                input_order=tuple(item.get("input_order", [])),
                inputs=tuple(item.get("inputs", [])),
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


def classify_pair(old_result: ReplayResult | dict | None, new_result: ReplayResult | dict | None,
                  condition: DifferentialCondition) -> str:
    old = _result_dict(old_result)
    new = _result_dict(new_result)
    if old is None or new is None:
        # One side never ran (e.g. the budget or wall clock cut the pair in
        # half).  The round is incomplete: it is not a hit, not a miss and
        # not a preparation failure of either version.
        return "incomplete"
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


def _result_dict(result: ReplayResult | dict | None) -> dict | None:
    if result is None:
        return None
    payload = result.to_dict() if isinstance(result, ReplayResult) else dict(result)
    return payload or None


# Runs one pair in the given execution order and returns each side's result
# keyed by version identity; a side maps to None when it never ran.
RunPair = Callable[
    [SessionSequence, str, str, tuple[str, str]],
    dict[str, ReplayResult | dict | None],
]


def _round_order(condition: DifferentialCondition, round_kind: str,
                 alternating_index: int) -> tuple[str, str]:
    forward = (condition.old.identity, condition.new.identity)
    if round_kind == "alternating" and alternating_index % 2 == 1:
        return (condition.new.identity, condition.old.identity)
    return forward


def evaluate_candidate(
    candidate: SessionSequence,
    condition: DifferentialCondition,
    run_pair: RunPair,
    policy: AttemptPolicy | None = None,
    name_prefix: str = "candidate",
    mode: str = "solo",
    order_plan: tuple[str, ...] = ("fixed",),
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
        if policy.stop_after_incomplete is not None and counts["incomplete"] >= policy.stop_after_incomplete:
            break
        attempt_name = f"{name_prefix}-pair-{len(evaluation.attempts)}"
        round_kind = order_plan[len(evaluation.attempts) % len(order_plan)]
        alternating_index = sum(1 for item in evaluation.attempts if item.round_kind == "alternating")
        order = _round_order(condition, round_kind, alternating_index)
        results = run_pair(candidate, attempt_name, mode, order)
        old_result = _result_dict(results.get(condition.old.identity))
        new_result = _result_dict(results.get(condition.new.identity))
        disposition = classify_pair(old_result, new_result, condition)
        evaluation.attempts.append(PairAttempt(
            name=attempt_name,
            mode=mode,
            round_kind=round_kind,
            input_order=order,
            inputs=tuple(step.data.hex() for step in candidate.steps),
            old_identity=condition.old.identity,
            new_identity=condition.new.identity,
            old_result=old_result or {},
            new_result=new_result or {},
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
        or (policy.stop_after_incomplete is not None
            and counts["incomplete"] >= policy.stop_after_incomplete)
    )
    if valid:
        ratio_ok = counts["hit"] / valid >= policy.required_hit_ratio
        orders_ok = (
            not policy.require_both_orders
            or evaluation.order_conclusion()["conclusion"] == "both_orders"
        )
        evaluation.accepted = ratio_ok and orders_ok
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
    order_plan: tuple[str, ...] = ("fixed",),
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
            order_plan=order_plan,
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
                order_plan=order_plan,
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
                        order_plan=order_plan,
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
