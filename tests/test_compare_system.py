from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from proto_robust.compare import (
    CompareCampaign,
    ComparePolicy,
    CompareSide,
    classify_candidate,
    rerun_candidate,
)
from proto_robust.compare_reducer import condition_holds, reduce_difference
from proto_robust.generator import SessionSequenceGenerator
from proto_robust.models import Budget, Outcome
from proto_robust.session_campaign import signature_from_result
from proto_robust.session_runner import replay_sequence_isolated
from proto_robust.sequences import SessionSequence

OLD = CompareSide("old", "targets.session_store.replay", "targets.session_store.describe_state")
NEW = CompareSide("new", "targets.session_store_v2.replay", "targets.session_store_v2.describe_state")
SEED = 20260929


def run_campaign(out: Path, cases: int, indexes, timeout: float = 0.5) -> CompareCampaign:
    campaign = CompareCampaign(
        OLD, NEW, Budget(max_cases=cases, timeout=timeout), out, SEED,
        ComparePolicy(valid_attempts=3, stop_on_first_hit=True), indexes=indexes,
    )
    campaign.run()
    return campaign


def load_candidate(out: Path, index: int) -> dict:
    return json.loads((out / f"candidate#{index}.json").read_text(encoding="utf-8"))


class CompareClassificationTests(unittest.TestCase):
    def test_old_reproducible_new_not_observed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "cmp"
            run_campaign(out, 200, [0])
            candidate = load_candidate(out, 0)
            self.assertEqual(candidate["classification"], "fixed_in_new")
            self.assertEqual(candidate["stage"], "done")
            old, new = candidate["sides"]["old"], candidate["sides"]["new"]
            # Every round kept target identity, start state and ordered inputs.
            self.assertEqual(old["trials"][0]["target"], OLD.replay)
            self.assertEqual(old["trials"][0]["start_state"], "closed")
            self.assertEqual([step["step"] for step in old["trials"][0]["steps"]],
                             [0, 1, 2, 3, 4])
            self.assertEqual(old["dispositions"].get("hit"), 1)
            # The new build explicitly rejects the trigger: a valid
            # observation, counted as not-observed rather than a defect.
            self.assertEqual(new["dispositions"].get("rejected"), 3)
            self.assertEqual(new["hits"], 0)
            self.assertEqual(candidate["difference_condition"]["kind"], "not_observed")

    def test_both_versions_fail_at_different_locations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "cmp"
            run_campaign(out, 60, [1])
            candidate = load_candidate(out, 1)
            self.assertEqual(candidate["classification"], "different_location")
            old_sig = candidate["sides"]["old"]["signature"]
            new_sig = candidate["sides"]["new"]["signature"]
            self.assertEqual(old_sig["exception_type"], "IndexError")
            self.assertEqual(new_sig["exception_type"], "IndexError")
            self.assertNotEqual(old_sig["fault_line"], new_sig["fault_line"])
            self.assertEqual(
                candidate["difference_condition"]["kind"], "different_location")

    def test_incomplete_session_prep_is_incomparable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "cmp"
            run_campaign(out, 60, [2])
            candidate = load_candidate(out, 2)
            self.assertEqual(candidate["classification"], "incomparable")
            new = candidate["sides"]["new"]
            # CONFIG is retired in v2: the build-up breaks before the trigger.
            self.assertTrue(new["prep_blocked"])
            self.assertGreaterEqual(new["dispositions"].get("prep_failed", 0), 1)
            prep_trial = next(t for t in new["trials"] if t["disposition"] == "prep_failed")
            self.assertLess(prep_trial["failing_step"], candidate["trigger_step"])

    def test_same_failure_on_both_versions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "cmp"
            run_campaign(out, 60, [3])
            candidate = load_candidate(out, 3)
            self.assertEqual(candidate["classification"], "same_location")


class CompareBudgetResumeTests(unittest.TestCase):
    def test_budget_pause_resumes_without_recounting_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "cmp"
            first = run_campaign(out, 6, [0])
            paused = load_candidate(out, 0)
            self.assertEqual(paused["stage"], "reducing")
            old_trials = len(paused["sides"]["old"]["trials"])
            new_trials = len(paused["sides"]["new"]["trials"])
            paused_steps = len(paused["reduction"]["steps"])

            resumed = CompareCampaign(
                OLD, NEW, Budget(max_cases=200, timeout=0.5), out, SEED,
                ComparePolicy(), indexes=[0],
            )
            summary = resumed.run()
            self.assertTrue(summary.resumed)
            final = load_candidate(out, 0)
            self.assertEqual(final["stage"], "done")
            self.assertEqual(final["classification"], "fixed_in_new")
            # Finished attempts were not re-run or double counted.
            self.assertEqual(len(final["sides"]["old"]["trials"]), old_trials)
            self.assertEqual(len(final["sides"]["new"]["trials"]), new_trials)
            self.assertGreaterEqual(len(final["reduction"]["steps"]), paused_steps)
            self.assertTrue(final["reduction"]["complete"])

            # The final candidate replays independently under both builds.
            minimized = SessionSequence.load(out / "candidate#0.minimized.sequence.json")
            old_result = replay_sequence_isolated(minimized, OLD.replay, OLD.describe, 0.5)
            new_result = replay_sequence_isolated(minimized, NEW.replay, NEW.describe, 0.5)
            self.assertEqual(old_result.outcome, Outcome.CRASH.value)
            self.assertEqual(old_result.state_path, "selected/mode=read/bucket=mirror")
            self.assertEqual(new_result.outcome, Outcome.REJECTED.value)


class CompareRerunTests(unittest.TestCase):
    def test_single_rerun_results_kept_alongside_group_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "cmp"
            run_campaign(out, 200, [0])
            candidate_path = out / "candidate#0.json"
            updated = rerun_candidate(candidate_path, attempts=2, timeout=0.5)
            self.assertEqual(updated["classification"], "fixed_in_new")
            self.assertEqual(updated["stage"], "done")
            sources = {t["source"] for t in updated["sides"]["old"]["trials"]}
            self.assertEqual(sources, {"campaign", "rerun-single"})
            # Reruns replay the reduced candidate under the original session
            # conditions: old still hits, new still does not observe it.
            self.assertEqual(updated["sides"]["old"]["dispositions"].get("hit"), 3)
            self.assertEqual(updated["sides"]["new"]["dispositions"].get("rejected"), 5)

    def test_unfinished_attempt_is_not_counted_as_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "cmp"
            run_campaign(out, 200, [0])
            candidate = load_candidate(out, 0)
            candidate_path = out / "candidate#0.json"
            # Simulate an interrupted attempt: recorded but incomplete.
            from proto_robust.compare import record_trial
            before = candidate["sides"]["new"]["valid_runs"]
            record_trial(candidate, "new", None, source="rerun-single",
                         completed=False, note="worker launch failed")
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            reloaded = load_candidate(out, 0)
            new = reloaded["sides"]["new"]
            self.assertEqual(new["valid_runs"], before)
            self.assertEqual(new["dispositions"].get("rejected"), before)
            self.assertEqual(new["trials"][-1]["disposition"], "incomplete")
            self.assertFalse(new["trials"][-1]["completed"])


class DifferenceReductionTests(unittest.TestCase):
    def test_reduction_rejects_candidates_that_collapse_the_difference(self) -> None:
        sequence = SessionSequenceGenerator(SEED).generate(0)
        old_result = replay_sequence_isolated(sequence, OLD.replay, OLD.describe, 0.5)
        old_sig = signature_from_result(old_result)
        condition = {"kind": "not_observed", "failing_side": "old"}

        def run_old(candidate):
            return replay_sequence_isolated(candidate, OLD.replay, OLD.describe, 0.5)

        def run_new(candidate):
            return replay_sequence_isolated(candidate, NEW.replay, NEW.describe, 0.5)

        report, _consumed, exhausted = reduce_difference(
            sequence, old_sig, condition, run_old, run_new,
            repeats=1, budget=10_000,
        )
        self.assertFalse(exhausted)
        self.assertTrue(report.complete)
        names = [step.name for step in report.minimized.steps]
        self.assertEqual(names, ["open", "prepare-mode-1", "select-bucket-2", "commit-overread"])
        # The dropped SET interaction was accepted only because BOTH sides
        # kept the difference; evidence from both builds is recorded.
        drop = next(step for step in report.steps if step["action"] == "drop-interaction")
        self.assertIn("failing", drop["evidence"])
        self.assertIn("other", drop["evidence"])
        self.assertGreater(drop["evidence"]["other"]["condition_holds"], 0)

    def test_condition_requires_other_side_to_stay_quiet(self) -> None:
        sequence = SessionSequenceGenerator(SEED).generate(0)
        new_result = replay_sequence_isolated(sequence, NEW.replay, NEW.describe, 0.5)
        self.assertTrue(condition_holds(new_result, {"kind": "not_observed"}, None))
        old_result = replay_sequence_isolated(sequence, OLD.replay, OLD.describe, 0.5)
        self.assertFalse(condition_holds(old_result, {"kind": "not_observed"}, None))


if __name__ == "__main__":
    unittest.main()
