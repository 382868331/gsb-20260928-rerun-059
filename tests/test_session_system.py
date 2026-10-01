from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from proto_robust.generator import SessionSequenceGenerator
from proto_robust.models import Budget, FailureSignature, Outcome
from proto_robust.session_campaign import (
    CONFIRM_REPEATS,
    SessionCampaign,
    signature_from_result,
)
from proto_robust.session_reducer import reduce_sequence, same_failure
from proto_robust.session_runner import replay_sequence_isolated
from proto_robust.sequences import Interaction, SessionSequence
from targets.session_store import frame, replay

REPLAY = "targets.session_store.replay"
DESCRIBE = "targets.session_store.describe_state"


def run(sequence: SessionSequence, timeout: float = 0.5, namespace: str = "confirm"):
    return replay_sequence_isolated(sequence, REPLAY, DESCRIBE, timeout,
                                    attempt_namespace=namespace)


def commit_defect_sequence(index: int = 0) -> SessionSequence:
    return SessionSequenceGenerator(20260929).generate(0)


class SessionReplayTests(unittest.TestCase):
    def test_defect_requires_cross_session_state_build_up(self) -> None:
        # The COMMIT frame alone is an explicit rejection from a clean session.
        lone_commit = SessionSequence(
            0, "lone-commit", "closed",
            (Interaction("commit", frame(0x50, 3, b"Z")),),
        )
        lone = run(lone_commit)
        self.assertIs(lone.outcome, Outcome.REJECTED.value)

        # The same frame after the state build-up triggers the crash.
        gated = commit_defect_sequence()
        result = run(gated)
        self.assertIs(result.outcome, Outcome.CRASH.value)
        self.assertEqual(result.exception_type, "IndexError")
        self.assertEqual(result.state_path, "selected/mode=read/bucket=mirror")
        self.assertEqual(result.failing_step, 4)
        self.assertEqual([step.name for step in result.steps],
                         ["open", "prepare-mode-1", "select-bucket-2", "set-context", "commit-overread"])
        self.assertTrue(all(step.state_before for step in result.steps))

    def test_full_session_start_and_order_kept_for_every_attempt(self) -> None:
        sequence = commit_defect_sequence()
        first = run(sequence)
        signature = signature_from_result(first)
        for _ in range(2):
            result = run(sequence)
            self.assertTrue(same_failure(result, signature))
            self.assertEqual(result.start_state, "closed")
            self.assertEqual([step.step for step in result.steps], [0, 1, 2, 3, 4])
            self.assertEqual(result.steps[0].state_before, "closed")
            self.assertEqual(result.steps[1].state_before, "open")
            self.assertEqual(result.steps[4].state_before, "selected")

    def test_timeout_rejection_and_crash_are_classified_separately(self) -> None:
        generator = SessionSequenceGenerator(20260929)
        timeout_result = run(generator.generate(2), timeout=0.5)
        panic_result = run(generator.generate(3), timeout=1.0)
        reject_result = run(SessionSequence(
            0, "lone-commit", "closed",
            (Interaction("commit", frame(0x50, 3)),),
        ))
        self.assertEqual(timeout_result.outcome, Outcome.TIMEOUT.value)
        self.assertEqual(timeout_result.steps[-1].name, "timing")
        self.assertEqual(panic_result.outcome, Outcome.RUNNER_ERROR.value)
        self.assertEqual(panic_result.exit_code, 9)
        self.assertEqual(panic_result.steps[-1].name, "panic")
        self.assertEqual(reject_result.outcome, Outcome.REJECTED.value)

        self.assertNotEqual(timeout_result.outcome, panic_result.outcome)
        self.assertNotEqual(panic_result.outcome, reject_result.outcome)

    def test_failed_worker_never_pollutes_next_sequence(self) -> None:
        generator = SessionSequenceGenerator(20260929)
        # A crash, a timeout and a native abort in sequence.
        crash = run(generator.generate(0))
        timeout = run(generator.generate(2))
        panic = run(generator.generate(3))
        self.assertEqual(crash.outcome, Outcome.CRASH.value)
        self.assertEqual(timeout.outcome, Outcome.TIMEOUT.value)
        self.assertEqual(panic.outcome, Outcome.RUNNER_ERROR.value)

        # Every subsequent sequence starts from closed; benign OPEN/CLOSE works.
        benign = generator.generate(5)
        result = run(benign)
        self.assertEqual(result.outcome, Outcome.OK.value)
        self.assertEqual(result.start_state, "closed")
        self.assertEqual(result.steps[0].state_before, "closed")
        self.assertEqual(result.end_state, "closed")

    def test_flaky_session_keeps_hit_and_miss_attempts(self) -> None:
        sequence = SessionSequenceGenerator(20260929).generate(4)
        outcomes = set()
        for attempt in range(4):
            candidate = run(sequence, namespace=f"confirm-{attempt}")
            outcomes.add(candidate.outcome)
            if candidate.outcome == Outcome.CRASH.value:
                # The intermittent hit stays in armed append state.
                self.assertEqual(candidate.state_path, "armed/mode=append")
                self.assertEqual(candidate.exception_type, "ValueError")
                self.assertTrue(candidate.steps[-1].name.startswith("flaky"))
        # Independent attempts (fresh processes) produce both a hit and a miss.
        self.assertEqual(outcomes, {Outcome.OK.value, Outcome.CRASH.value})

    def test_reducer_removes_interactions_and_bytes_but_keeps_state_path(self) -> None:
        sequence = commit_defect_sequence()
        signature = signature_from_result(run(sequence))
        report, _consumed, exhausted = reduce_sequence(
            sequence, signature, lambda candidate: run(candidate), repeats=1, budget=10_000
        )
        self.assertFalse(exhausted)
        self.assertTrue(report.complete)
        names = [step.name for step in report.minimized.steps]
        # The non-essential SET interaction is removed; the ordered state build-up stays.
        self.assertNotIn("set-context", names)
        self.assertEqual(names, ["open", "prepare-mode-1", "select-bucket-2", "commit-overread"])
        actions = {step["action"] for step in report.steps}
        self.assertIn("drop-interaction", actions)
        self.assertIn("lower-byte", actions)
        check = run(report.minimized)
        self.assertEqual(check.fault_line, signature.fault_line)
        self.assertEqual(check.state_path, signature.state_path)

    def test_reducer_does_not_collapse_into_other_indexerror_site(self) -> None:
        commit_sequence = commit_defect_sequence()
        commit_signature = signature_from_result(run(commit_sequence))
        mode_sequence = SessionSequenceGenerator(20260929).generate(1)
        mode_signature = signature_from_result(run(mode_sequence))
        # Two IndexError defects live at different lines and state paths.
        self.assertNotEqual(commit_signature.fault_line, mode_signature.fault_line)
        self.assertNotEqual(commit_signature.state_path, mode_signature.state_path)
        self.assertNotEqual(commit_signature.key(), mode_signature.key())

        # The shorter mode-table sequence is NOT the same failure.
        shorter = SessionSequence(
            0, "open-then-commit", "closed",
            (Interaction("open", frame(0x10)), Interaction("commit", frame(0x50, 3))),
        )
        self.assertFalse(same_failure(run(shorter), commit_signature))


class SessionCampaignTests(unittest.TestCase):
    def test_budget_pause_resumes_reduction_and_keeps_both_replays(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "session"
            first = SessionCampaign(
                REPLAY, DESCRIBE,
                Budget(max_cases=7, timeout=0.5),
                out, 20260929, indexes=[0],
            )
            summary = first.run()
            self.assertTrue(summary.budget_exhausted)

            finding_records = [path for path in out.glob("crash:IndexError*#0.json")]
            self.assertEqual(len(finding_records), 1)
            stem = finding_records[0].name[:-5]
            original_path = out / f"{stem}.original.sequence.json"
            minimized_path = out / f"{stem}.minimized.sequence.json"
            self.assertTrue(original_path.exists())
            self.assertTrue(minimized_path.exists())
            original = SessionSequence.load(original_path)
            paused = SessionSequence.load(minimized_path)
            self.assertEqual(len(original.steps), 5)
            self.assertLessEqual(len(paused.steps), len(original.steps))

            finding_path = out / f"{stem}.json"
            paused_finding = json.loads(finding_path.read_text(encoding="utf-8"))
            self.assertEqual(paused_finding["stage"], "reducing")
            paused_steps = paused_finding["reduction"]["steps"]
            self.assertTrue(paused_steps)

            # Resume with a fresh budget; reduction continues to completion.
            resumed = SessionCampaign(
                REPLAY, DESCRIBE,
                Budget(max_cases=40, timeout=0.5),
                out, 20260929, indexes=[0],
            )
            resumed_summary = resumed.run()
            self.assertTrue(resumed_summary.resumed)

            final_finding = json.loads(finding_path.read_text(encoding="utf-8"))
            self.assertEqual(final_finding["stage"], "reduced")
            self.assertTrue(final_finding["reduction"]["complete"])
            # Reduction steps accumulated across the pause rather than restarting.
            self.assertGreaterEqual(
                len(final_finding["reduction"]["steps"]), len(paused_steps)
            )

            # Original and minimum stay independently replayable.
            minimum = SessionSequence.load(minimized_path)
            self.assertEqual(
                [step.name for step in minimum.steps],
                ["open", "prepare-mode-1", "select-bucket-2", "commit-overread"],
            )
            original_result = run(original)
            minimum_result = run(minimum)
            self.assertEqual(original_result.state_path, minimum_result.state_path)
            self.assertEqual(original_result.fault_line, minimum_result.fault_line)


    def test_campaign_records_confirmation_attempts_with_hit_rate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "session"
            campaign = SessionCampaign(
                REPLAY, DESCRIBE,
                Budget(max_cases=20, timeout=0.5),
                out, 20260929,
            )
            campaign.run()
            findings = list(out.glob("crash*#0.json"))
            self.assertEqual(len(findings), 1)
            payload = json.loads(findings[0].read_text(encoding="utf-8"))
            confirmation = payload["confirmation"]
            self.assertEqual(confirmation["runs"], CONFIRM_REPEATS)
            self.assertEqual(confirmation["hits"], CONFIRM_REPEATS)
            self.assertEqual(confirmation["reproduction_rate"], 1.0)
            self.assertEqual(len(confirmation["trials"]), CONFIRM_REPEATS)
            first_trial = confirmation["trials"][0]
            self.assertIn("steps", first_trial)
            self.assertEqual(first_trial["start_state"], "closed")

    def test_intermittent_finding_keeps_evidence_without_reducing(self) -> None:
        # Confirmation attempts use deterministic parity confirm-0..4:
        # hit/miss/hit/miss/hit = 3/5, which is intermittent (below the strict
        # majority stability threshold) and is kept with evidence, not reduced.
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "flaky"
            campaign = SessionCampaign(
                REPLAY, DESCRIBE,
                Budget(max_cases=20, timeout=1.0),
                out, 20260929,
                indexes=[4],
            )
            campaign.run()
            records = list(out.glob("crash:ValueError*#4.json"))
            self.assertEqual(len(records), 1)
            payload = json.loads(records[0].read_text(encoding="utf-8"))
            confirmation = payload["confirmation"]
            self.assertEqual(confirmation["runs"], 5)
            outcomes = {trial["outcome"] for trial in confirmation["trials"]}
            self.assertEqual(outcomes, {Outcome.OK.value, Outcome.CRASH.value})
            self.assertLess(confirmation["hits"], confirmation["runs"])
            self.assertFalse(confirmation["reproduction_rate"] >= 1.0)
            # Below the stability threshold: original kept, not reduced.
            self.assertEqual(payload["stage"], "kept_unstable")
            self.assertFalse(payload.get("reduction"))
            stem = payload["stem"]
            self.assertTrue((out / f"{stem}.original.sequence.json").exists())


if __name__ == "__main__":
    unittest.main()
