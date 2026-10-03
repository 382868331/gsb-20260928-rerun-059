from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from proto_robust.differential import (
    AttemptPolicy,
    DifferenceKind,
    VersionSpec,
    classify_pair,
    evaluate_candidate,
    order_conclusion,
)
from proto_robust.differential_campaign import DifferentialCampaign
from proto_robust.models import Budget, Outcome
from proto_robust.session_campaign import signature_from_result
from proto_robust.session_runner import replay_sequence_isolated
from proto_robust.sequences import ReplayResult, SessionSequence

OLD = VersionSpec("old", "targets.session_store.replay", "targets.session_store.describe_state")
NEW = VersionSpec("new", "targets.session_store_new.replay", "targets.session_store.describe_state")
PREP = VersionSpec("prep", "targets.session_store_prep.replay", "targets.session_store.describe_state")


def run(sequence: SessionSequence, version: VersionSpec, namespace: str = "compare"):
    return replay_sequence_isolated(
        sequence, version.replay_name, version.describe_name, 0.5,
        attempt_namespace=namespace,
    )


class DifferentialComparisonTests(unittest.TestCase):
    def setUp(self) -> None:
        path = Path("results/session-core-demo/crash:IndexError@replay:193#selected_mode=read_bucket=mirror#0.original.sequence.json")
        self.sequence = SessionSequence.load(path)

    def test_old_failure_new_clean_rejection_is_reduced_with_full_pair_evidence(self) -> None:
        old_result = run(self.sequence, OLD)
        new_result = run(self.sequence, NEW)
        condition = DifferentialConditionFactory.old_only(old_result, NEW)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "differential"
            summary = DifferentialCampaign(
                self.sequence, condition, Budget(80, 0.5), out, 20261002,
                candidate_policy=AttemptPolicy(1, stop_after_rejections=1),
                final_policy=AttemptPolicy(1),
            ).run()
            self.assertEqual(summary.accepted_candidates, 1)
            final = SessionSequence.load(out / "final.sequence.json")
            self.assertEqual(
                [step.name for step in final.steps],
                ["open", "prepare-mode-1", "select-bucket-2", "commit-overread"],
            )
            old_final = run(final, OLD, "final-old")
            new_final = run(final, NEW, "final-new")
            self.assertEqual(old_final.outcome, Outcome.CRASH.value)
            self.assertEqual(old_final.fault_line, old_result.fault_line)
            self.assertEqual(new_final.outcome, Outcome.REJECTED.value)
            payload = json.loads((out / "differential.json").read_text(encoding="utf-8"))
            self.assertIn("hit", payload["outcomes"])
            self.assertIn("rejected", payload["outcomes"])
            self.assertEqual(payload["input_order"], ["old", "new"])

    def test_preparation_failure_is_not_a_miss_or_rejection(self) -> None:
        old_result = run(self.sequence, OLD)
        prep_result = run(self.sequence, PREP)
        condition = DifferentialConditionFactory.old_only(old_result, PREP)
        disposition = classify_pair(old_result, prep_result, condition)
        self.assertEqual(disposition, "preparation_failed")

        evaluation = evaluate_candidate(
            self.sequence, condition,
            lambda candidate, name, mode, versions: (run(candidate, OLD, name), run(candidate, PREP, name)),
            AttemptPolicy(2, stop_after_preparation_failures=1),
            "prep-check", "solo",
        )
        self.assertTrue(evaluation.complete)
        self.assertFalse(evaluation.accepted)
        self.assertEqual(evaluation.counts(), {
            "hit": 0,
            "rejected": 0,
            "timeout": 0,
            "preparation_failed": 1,
            "incomplete": 0,
        })

    def test_different_location_requires_both_selected_signatures(self) -> None:
        old_result = ReplayResult(
            outcome=Outcome.CRASH.value, start_state="closed", end_state="selected",
            duration=0.0, exception_type="IndexError", fault_line=100,
            state_path="selected/mode=read", key_conditions=((2, 0, 0x50), (2, 1, 3)),
        )
        new_result = ReplayResult(
            outcome=Outcome.CRASH.value, start_state="closed", end_state="selected",
            duration=0.0, exception_type="IndexError", fault_line=200,
            state_path="selected/mode=read", key_conditions=((2, 0, 0x50), (2, 1, 3)),
        )
        same_site = ReplayResult(
            outcome=Outcome.CRASH.value, start_state="closed", end_state="selected",
            duration=0.0, exception_type="IndexError", fault_line=100,
            state_path="selected/mode=read", key_conditions=((2, 0, 0x50), (2, 1, 3)),
        )
        condition = DifferentialConditionFactory.locations(old_result, NEW, new_result)
        self.assertEqual(classify_pair(old_result, new_result, condition), "hit")
        self.assertEqual(classify_pair(old_result, same_site, condition), "rejected")

    def test_budget_resume_does_not_replay_existing_pair_attempts(self) -> None:
        old_result = run(self.sequence, OLD)
        condition = DifferentialConditionFactory.old_only(old_result, NEW)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "resume"
            first = DifferentialCampaign(
                self.sequence, condition, Budget(2, 0.5), out, 20261002,
            ).run()
            self.assertTrue(first.budget_exhausted)
            self.assertEqual(first.pair_attempts, 2)
            resumed = DifferentialCampaign(
                self.sequence, condition, Budget(80, 0.5), out, 20261002,
            ).run()
            self.assertTrue(resumed.resumed)
            payload = json.loads((out / "differential.json").read_text(encoding="utf-8"))
            self.assertEqual(resumed.accepted_candidates, 1)
            self.assertEqual(payload["pair_attempts"], resumed.pair_attempts)
            self.assertTrue((out / "original.sequence.json").exists())
            self.assertTrue((out / "final.sequence.json").exists())


class DifferentialConditionFactory:
    @staticmethod
    def old_only(result: ReplayResult, new_version: VersionSpec):
        from proto_robust.differential import DifferentialCondition
        return DifferentialCondition(
            DifferenceKind.OLD_ONLY, OLD, new_version,
            signature_from_result(result),
        )

    @staticmethod
    def locations(old_result: ReplayResult, new_version: VersionSpec, new_result: ReplayResult):
        from proto_robust.differential import DifferentialCondition
        return DifferentialCondition(
            DifferenceKind.DIFFERENT_LOCATION, OLD, new_version,
            signature_from_result(old_result), signature_from_result(new_result),
        )


def _crash_result(line: int = 100) -> ReplayResult:
    return ReplayResult(
        outcome=Outcome.CRASH.value, start_state="closed", end_state="selected",
        duration=0.0, exception_type="IndexError", fault_line=line,
        state_path="selected/mode=read", key_conditions=((2, 0, 0x50), (2, 1, 3)),
    )


def _clean_result() -> ReplayResult:
    return ReplayResult(
        outcome=Outcome.REJECTED.value, start_state="closed", end_state="selected",
        duration=0.0,
    )


class PairedOrderEffectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.condition = DifferentialConditionFactory.old_only(_crash_result(), NEW)

    def test_alternating_rounds_record_execution_order_and_start_points(self) -> None:
        def run_pair(candidate, name, mode, versions):
            self.assertEqual([v.identity for v in versions],
                             ["old", "new"] if "-0" in name or "-2" in name else ["new", "old"])
            return _crash_result(), _clean_result()

        policy = AttemptPolicy(valid_attempts=3, order_mode="alternating",
                               stop_after_timeouts=None, stop_after_preparation_failures=None)
        evaluation = evaluate_candidate(
            SessionSequence(0, "demo", "closed", ()),
            self.condition, run_pair, policy, "alt", "group",
        )
        self.assertTrue(evaluation.accepted)
        orders = [tuple(attempt.execution_order) for attempt in evaluation.attempts]
        self.assertEqual(orders, [("old", "new"), ("new", "old"), ("old", "new")])
        for attempt in evaluation.attempts:
            self.assertEqual(attempt.input_order, ("old", "new"))
            self.assertEqual(attempt.old_start_state, "closed")
            self.assertEqual(attempt.new_start_state, "closed")
            self.assertTrue(attempt.old_prepared)
            self.assertTrue(attempt.new_prepared)
        conclusion = order_conclusion(evaluation.attempts)
        self.assertEqual(conclusion["verdict"], "both_orders")
        self.assertFalse(conclusion["order_conditioned"])

    def test_difference_visible_in_only_one_order_is_flagged_order_dependent(self) -> None:
        def run_pair(candidate, name, mode, versions):
            # The new version only shows the fault when it runs first:
            # the old run no longer changes its starting conditions here,
            # so the pair is a hit only in the old->new order.
            if versions[0].identity == "new":
                return _crash_result(), _crash_result()
            return _crash_result(), _clean_result()

        policy = AttemptPolicy(valid_attempts=2, order_mode="alternating",
                               stop_after_rejections=None, stop_after_timeouts=None,
                               stop_after_preparation_failures=None)
        evaluation = evaluate_candidate(
            SessionSequence(0, "demo", "closed", ()),
            self.condition, run_pair, policy, "order", "group",
        )
        counts = evaluation.counts()
        self.assertEqual(counts["hit"], 1)
        self.assertEqual(counts["rejected"], 1)
        conclusion = order_conclusion(evaluation.attempts)
        self.assertEqual(conclusion["verdict"], "order_dependent:old->new")
        self.assertTrue(conclusion["order_conditioned"])

    def test_incomplete_round_is_not_a_miss_and_stays_separate(self) -> None:
        rounds = []

        def run_pair(candidate, name, mode, versions):
            rounds.append(name)
            if len(rounds) == 1:
                return _crash_result(), None
            return _crash_result(), _clean_result()

        policy = AttemptPolicy(valid_attempts=2, stop_after_timeouts=None,
                               stop_after_preparation_failures=None)
        evaluation = evaluate_candidate(
            SessionSequence(0, "demo", "closed", ()),
            self.condition, run_pair, policy, "mixed", "group",
        )
        self.assertTrue(evaluation.complete)
        self.assertTrue(evaluation.accepted)
        counts = evaluation.counts()
        self.assertEqual(counts["incomplete"], 1)
        self.assertEqual(counts["hit"], 2)
        self.assertEqual(len(evaluation.valid_attempts), 2)
        self.assertEqual(evaluation.attempts[0].disposition, "incomplete")
        self.assertEqual(evaluation.attempts[0].new_result, {})

    def test_campaign_alternating_order_summary_and_solo_identity(self) -> None:
        path = Path("results/session-core-demo/crash:IndexError@replay:193#selected_mode=read_bucket=mirror#0.original.sequence.json")
        sequence = SessionSequence.load(path)
        old_result = run(sequence, OLD)
        condition = DifferentialConditionFactory.old_only(old_result, NEW)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "alternating"
            summary = DifferentialCampaign(
                sequence, condition, Budget(240, 0.5), out, 20261003,
                candidate_policy=AttemptPolicy(2, stop_after_rejections=1,
                                               order_mode="alternating"),
                final_policy=AttemptPolicy(2, order_mode="alternating"),
            ).run()
            self.assertEqual(summary.accepted_candidates, 1)
            payload = json.loads((out / "differential.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["order_mode"], "alternating")
            self.assertIn("valid", payload["outcomes"])
            self.assertIn("incomplete", payload["outcomes"])
            self.assertEqual(payload["order_conclusion"]["verdict"], "both_orders")
            self.assertEqual(payload["order_conditioned_notes"], [])
            checkpoint = json.loads((out / "differential.checkpoint.json").read_text(encoding="utf-8"))
            baseline = checkpoint["reduction"]["steps"][0]
            self.assertEqual({attempt["mode"] for attempt in baseline["attempts"]}, {"solo"})
            self.assertEqual(
                {attempt["mode"] for attempt in checkpoint["final"]["attempts"]}, {"final"})
            orders = {tuple(attempt["execution_order"])
                      for attempt in checkpoint["final"]["attempts"]}
            self.assertEqual(orders, {("old", "new"), ("new", "old")})
            final = SessionSequence.load(out / "final.sequence.json")
            self.assertEqual(run(final, OLD, "recheck-old").outcome, Outcome.CRASH.value)


if __name__ == "__main__":
    unittest.main()
