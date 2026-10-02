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
            lambda candidate, name, mode: (run(candidate, OLD, name), run(candidate, PREP, name)),
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


if __name__ == "__main__":
    unittest.main()
