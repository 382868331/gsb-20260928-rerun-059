from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from proto_robust.differential import (
    AttemptPolicy,
    DifferentialCondition,
    DifferenceKind,
    VersionSpec,
    classify_pair,
    evaluate_candidate,
)
from proto_robust.differential_campaign import DifferentialCampaign
from proto_robust.models import Budget, Outcome
from proto_robust.session_campaign import signature_from_result
from proto_robust.session_runner import replay_sequence_isolated
from proto_robust.sequences import ReplayResult, SessionSequence

OLD = VersionSpec("old", "targets.session_store.replay", "targets.session_store.describe_state")
NEW = VersionSpec("new", "targets.session_store_new.replay", "targets.session_store.describe_state")
PREP = VersionSpec("prep", "targets.session_store_prep.replay", "targets.session_store.describe_state")
SHARED_OLD = VersionSpec("old", "targets.session_store_shared.replay",
                         "targets.session_store.describe_state")
SHARED_NEW = VersionSpec("new", "targets.session_store_shared_new.replay",
                         "targets.session_store.describe_state")


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
            lambda candidate, name, mode, order: {
                "old": run(candidate, OLD, name),
                "prep": run(candidate, PREP, name),
            },
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


class PairedOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        path = Path("results/session-core-demo/crash:IndexError@replay:193#selected_mode=read_bucket=mirror#0.original.sequence.json")
        self.sequence = SessionSequence.load(path)

    def _pair_runner(self, old: VersionSpec, new: VersionSpec):
        versions = {old.identity: old, new.identity: new}

        def run_pair(candidate, name, mode, order):
            results = {}
            for ordinal, identity in enumerate(order):
                version = versions[identity]
                results[identity] = run(candidate, version, f"{name}-{identity}-{ordinal}")
            return results

        return run_pair

    def test_alternating_rounds_record_order_inputs_and_start_states(self) -> None:
        old_result = run(self.sequence, OLD)
        condition = DifferentialConditionFactory.old_only(old_result, NEW)
        evaluation = evaluate_candidate(
            self.sequence, condition, self._pair_runner(OLD, NEW),
            AttemptPolicy(4, stop_after_timeouts=None, stop_after_preparation_failures=None),
            "ordered", "group",
            order_plan=("fixed", "alternating"),
        )
        self.assertTrue(evaluation.complete)
        kinds = [attempt.round_kind for attempt in evaluation.attempts]
        self.assertEqual(kinds, ["fixed", "alternating", "fixed", "alternating"])
        orders = [attempt.input_order for attempt in evaluation.attempts]
        self.assertEqual(orders, [("old", "new"), ("old", "new"), ("old", "new"), ("new", "old")])
        for attempt in evaluation.attempts:
            self.assertEqual(attempt.inputs, tuple(step.data.hex() for step in self.sequence.steps))
            payload = attempt.to_dict()
            self.assertEqual(payload["old_start_state"], "closed")
            self.assertEqual(payload["new_start_state"], "closed")
            self.assertTrue(payload["old_prepared"])
            self.assertTrue(payload["new_prepared"])
        conclusion = evaluation.order_conclusion()
        self.assertEqual(conclusion["conclusion"], "both_orders")
        self.assertFalse(conclusion["order_limited"])
        self.assertEqual(set(conclusion["per_order"]), {"old->new", "new->old"})

    def test_incomplete_round_is_listed_separately_and_not_a_miss(self) -> None:
        old_result = run(self.sequence, OLD)
        condition = DifferentialConditionFactory.old_only(old_result, NEW)

        def half_pair(candidate, name, mode, order):
            return {"old": run(candidate, OLD, name), "new": None}

        evaluation = evaluate_candidate(
            self.sequence, condition, half_pair,
            AttemptPolicy(2, stop_after_timeouts=None, stop_after_preparation_failures=None),
            "cut", "group",
        )
        self.assertEqual(evaluation.counts()["incomplete"], 1)
        self.assertEqual(evaluation.counts()["hit"], 0)
        self.assertEqual(len(evaluation.valid_attempts), 0)
        self.assertFalse(evaluation.accepted)
        self.assertEqual(
            evaluation.order_conclusion()["conclusion"],
            "no_valid_rounds",
        )

    def test_shared_backend_makes_difference_order_conditional(self) -> None:
        old_result = run(self.sequence, SHARED_OLD, "shared-setup")
        self.assertEqual(old_result.outcome, Outcome.CRASH.value)
        condition = DifferentialCondition(
            DifferenceKind.OLD_ONLY, SHARED_OLD, SHARED_NEW,
            signature_from_result(old_result),
        )
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["SESSION_STORE_SHARED_DIR"] = tmp
            try:
                evaluation = evaluate_candidate(
                    self.sequence, condition, self._pair_runner(SHARED_OLD, SHARED_NEW),
                    AttemptPolicy(2, stop_after_timeouts=None,
                                  stop_after_preparation_failures=None),
                    "shared", "group",
                    order_plan=("fixed", "alternating"),
                )
            finally:
                os.environ.pop("SESSION_STORE_SHARED_DIR", None)
        counts = evaluation.counts()
        # Old-first rounds: the old crash locks the shared backend, so the
        # new side never finishes preparation -- not a miss for the fix.
        self.assertEqual(counts["preparation_failed"], 6)
        # New-first rounds: backend still free, difference reproduces.
        self.assertEqual(counts["hit"], 2)
        self.assertEqual(counts["rejected"], 0)
        conclusion = evaluation.order_conclusion()
        self.assertTrue(conclusion["order_limited"])
        self.assertEqual(conclusion["conclusion"], "single_order_only")
        self.assertEqual(conclusion["per_order"]["new->old"]["hit"], 2)
        self.assertEqual(conclusion["per_order"]["old->new"]["preparation_failed"], 6)
        for attempt in evaluation.attempts:
            payload = attempt.to_dict()
            if attempt.input_order == ("old", "new"):
                self.assertFalse(payload["new_prepared"])
            else:
                self.assertTrue(payload["new_prepared"])

    def test_campaign_resume_keeps_order_plan_and_reports_order_conclusion(self) -> None:
        old_result = run(self.sequence, OLD)
        condition = DifferentialConditionFactory.old_only(old_result, NEW)
        policy = AttemptPolicy(2, stop_after_rejections=1)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "ordered"
            first = DifferentialCampaign(
                self.sequence, condition, Budget(2, 0.5), out, 20261003,
                candidate_policy=policy, final_policy=AttemptPolicy(2),
                order_plan=("fixed", "alternating"),
            ).run()
            self.assertTrue(first.budget_exhausted)
            self.assertEqual(first.pair_attempts, 2)
            resumed = DifferentialCampaign(
                self.sequence, condition, Budget(120, 0.5), out, 20261003,
                candidate_policy=policy, final_policy=AttemptPolicy(4),
                order_plan=("fixed", "alternating"),
            ).run()
            self.assertTrue(resumed.resumed)
            # Already-recorded rounds are not counted again after resuming.
            self.assertGreater(resumed.pair_attempts, first.pair_attempts)
            payload = json.loads((out / "differential.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["order_plan"], ["fixed", "alternating"])
            self.assertIn("incomplete", payload["outcomes"])
            self.assertIsNotNone(payload["order_conclusion"])
            self.assertEqual(
                payload["order_conclusion"]["conclusion"], "both_orders",
            )
            self.assertEqual(payload["order_limited_conclusions"], [])
            # The kept candidate replays from its recorded start point.
            final = SessionSequence.load(out / "final.sequence.json")
            replayed = run(final, OLD, "recheck")
            self.assertEqual(replayed.outcome, Outcome.CRASH.value)
            self.assertEqual(replayed.start_state, final.start_state)
            # Solo baseline and paired reduction evidence keep separate identities.
            checkpoint = json.loads((out / "differential.checkpoint.json").read_text(encoding="utf-8"))
            baseline = checkpoint["reduction"]["steps"][0]
            self.assertEqual(baseline["attempts"][0]["mode"], "solo")
            final_modes = {attempt["mode"] for attempt in checkpoint["final"]["attempts"]}
            self.assertEqual(final_modes, {"group"})
            final_names = [attempt["name"] for attempt in checkpoint["final"]["attempts"]]
            self.assertTrue(all(name.startswith("final-pair-") for name in final_names))


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


if __name__ == "__main__":
    unittest.main()
