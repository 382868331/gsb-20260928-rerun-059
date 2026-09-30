from __future__ import annotations

import unittest

from proto_robust.generator import SimpleMessageGenerator
from proto_robust.fuzzer import FuzzingCampaign
from proto_robust.models import Budget
from proto_robust.models import FailureSignature, Finding, Outcome, SessionSequence, TargetResult
from proto_robust.reducer import reduce_case
from proto_robust.runner import IsolatedWorker, run_isolated
from proto_robust.stability import confirm_failure
from targets.simple_message import parse_message


def slow_target(_: bytes) -> None:
    if _ == b"fast":
        return
    import time

    time.sleep(2.0)


def native_exit_target(_: bytes) -> None:
    if _ == b"survive":
        return
    import os

    os._exit(7)


def quiet_target(_: bytes) -> None:
    return None


FLAKY_STATE = "/tmp/proto-robust-flaky-count"


def flaky_target(_: bytes) -> None:
    from pathlib import Path

    path = Path(FLAKY_STATE)
    count = int(path.read_text()) if path.exists() else 0
    count += 1
    path.write_text(str(count))
    if count % 2 == 1:
        raise ValueError("intermittent parse defect")


def always_value_error(_: bytes) -> None:
    raise ValueError("deterministic parse defect")


class SimpleMessageSystemTests(unittest.TestCase):
    def test_generator_is_seed_deterministic(self) -> None:
        first = [case.data for case, _ in zip(SimpleMessageGenerator(7), range(40), strict=False)]
        second = [case.data for case, _ in zip(SimpleMessageGenerator(7), range(40), strict=False)]
        self.assertEqual(first, second)

    def test_rejection_is_distinct_from_crash(self) -> None:
        rejected = run_isolated(parse_message, b"XX", 1.0)
        crashing = run_isolated(parse_message, b"SM\x01\x00\x00\x01\xff", 1.0)
        self.assertIs(rejected.outcome, Outcome.REJECTED)
        self.assertIs(crashing.outcome, Outcome.CRASH)
        self.assertEqual(crashing.exception_type, "IndexError")

    def test_reducer_preserves_crash_type_and_key_byte(self) -> None:
        original = b"SM\x01\x00\x00\x05\xffAAAA"
        signature = FailureSignature(Outcome.CRASH, "IndexError", "list index out of range")

        def run(sequence: SessionSequence) -> TargetResult:
            try:
                parse_message(sequence.flattened())
            except Exception as exc:  # ParseReject intentionally changes outcome.
                outcome = Outcome.CRASH if type(exc).__name__ == "IndexError" else Outcome.REJECTED
                return TargetResult(outcome, 0.0, type(exc).__name__, str(exc))
            return TargetResult(Outcome.OK, 0.0)

        report = reduce_case(original, signature, run, repeats=1)
        minimized = report.minimized.flattened()
        self.assertEqual(minimized, b"SM\x01\x00\x00\x01\xff")
        self.assertIn({"type": "byte", "interaction": 0, "offset": 6, "value": 255,
                       "label": "message", "required": True}, report.critical_positions)
        self.assertTrue(report.steps)
        self.assertEqual(report.steps[0]["action"], "baseline")
        self.assertTrue(all(step["accepted"] for step in report.steps))
        self.assertTrue(all(step["reason"] for step in report.steps))

    def test_timeout_signature_differs_from_crash(self) -> None:
        self.assertNotEqual(
            FailureSignature(Outcome.TIMEOUT).key(),
            FailureSignature(Outcome.CRASH, "IndexError").key(),
        )

    def test_worker_recovers_after_timeout_and_process_death(self) -> None:
        worker = IsolatedWorker("tests.test_system.slow_target")
        try:
            timeout = worker.run(b"x", 0.05)
            self.assertIs(timeout.outcome, Outcome.TIMEOUT)
            recovered = worker.run(b"fast", 1.0)
            self.assertIs(recovered.outcome, Outcome.OK)
            worker.close()

            dead = IsolatedWorker("tests.test_system.native_exit_target")
            failed = dead.run(b"x", 1.0)
            self.assertIs(failed.outcome, Outcome.RUNNER_ERROR)
            self.assertIn("7", failed.message)
            restarted = dead.run(b"survive", 1.0)
            self.assertIs(restarted.outcome, Outcome.OK)
        finally:
            worker.close()
            if 'dead' in locals():
                dead.close()

    def test_two_same_exception_defects_group_separately_by_location(self) -> None:
        operation_site = run_isolated(parse_message, b"SM\x01\x00\x00\x01\xff", 1.0)
        channel_site = run_isolated(parse_message, b"SM\x01\x00\x00\x02\x01\xfe", 1.0)
        self.assertIs(operation_site.outcome, Outcome.CRASH)
        self.assertIs(channel_site.outcome, Outcome.CRASH)
        self.assertEqual(operation_site.exception_type, "IndexError")
        self.assertEqual(channel_site.exception_type, "IndexError")
        self.assertEqual(operation_site.fault_function, "parse_message")
        self.assertEqual(channel_site.fault_function, "parse_message")
        self.assertNotEqual(operation_site.fault_line, channel_site.fault_line)
        self.assertNotEqual(operation_site.signature.key(), channel_site.signature.key())

    def test_reducer_keeps_channel_defect_location_and_condition(self) -> None:
        original = b"SM\x01\x00\x00\x05\x01\xfeAAA"
        from targets.simple_message import replay_session

        first = run_isolated(replay_session, SessionSequence.from_bytes(original), 1.0)
        signature = first.signature
        report = reduce_case(original, signature,
                             lambda data: run_isolated(replay_session, data, 1.0), repeats=1)
        self.assertEqual(report.minimized.flattened(), b"SM\x01\x00\x00\x02\x01\xfe")
        # The reduction must not have collapsed into the other IndexError site.
        check = run_isolated(replay_session, report.minimized, 1.0)
        self.assertEqual(check.fault_line, signature.fault_line)
        self.assertTrue(any(
            position["type"] == "byte" and position["offset"] == 7
            for position in report.critical_positions
        ))

    def test_flaky_failure_keeps_all_repeat_evidence(self) -> None:
        from pathlib import Path

        Path(FLAKY_STATE).write_text("0")
        signature = FailureSignature(Outcome.CRASH, "ValueError", "intermittent parse defect")
        worker = IsolatedWorker("tests.test_system.flaky_target")
        try:
            evidence = confirm_failure(lambda: worker.run(b"x", 1.0), signature, 6)
        finally:
            worker.close()
        self.assertEqual(evidence.runs, 6)
        self.assertEqual(evidence.hits, 3)
        self.assertAlmostEqual(evidence.rate, 0.5)
        self.assertEqual(
            {trial["outcome"] for trial in evidence.trials},
            {Outcome.CRASH.value, Outcome.OK.value},
        )

    def test_campaign_groups_reduces_times_out_and_exhausts_budget(self) -> None:
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run"
            campaign = FuzzingCampaign(
                "targets.simple_message.replay_session",
                Budget(max_cases=10, timeout=0.5),
                out,
                20260929,
            )
            summary = campaign.run()
            self.assertTrue(summary.budget_exhausted)
            self.assertEqual(summary.cases_executed, 10)
            # Indexes 8 and 9 are the two distinct IndexError sites; only one
            # case is needed to exhaust the small budget, so the timeout at
            # index 10 is discovered after resume.
            summary_payload = json.loads((out / "campaign.json").read_text(encoding="utf-8"))
            self.assertTrue(summary_payload["budget_exhausted"])

            resumed = FuzzingCampaign(
                "targets.simple_message.replay_session",
                Budget(max_cases=1, timeout=0.5),
                out,
                20260929,
            )
            resumed_summary = resumed.run()
            self.assertTrue(resumed_summary.resumed)
            # First run consumed 10 cases; the resumed run consumed one more
            # (the timing defect at index 10), so the cumulative count is 11
            # and the original samples from the exhausted run are retained.
            self.assertEqual(resumed_summary.cases_executed, 11)

            payload = json.loads((out / "campaign.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["findings"], 3)
            finding_files = sorted(out.glob("*.json"))
            names = [path.name for path in finding_files if path.name != "campaign.json"]
            self.assertTrue(any("IndexError" in name and "#8" in name for name in names))
            self.assertTrue(any("IndexError" in name and "#9" in name for name in names))
            self.assertTrue(any(name.startswith("timeout#10") for name in names))

            operation = json.loads(
                (out / next(name for name in names if "#8" in name)).read_text(encoding="utf-8")
            )
            channel = json.loads(
                (out / next(name for name in names if "#9" in name)).read_text(encoding="utf-8")
            )
            self.assertNotEqual(
                operation["signature"]["fault_line"],
                channel["signature"]["fault_line"],
            )
            # Original sample and every reduction step with a reason survive resume.
            self.assertEqual(operation["original_hex"], "534d01000001ff")
            self.assertTrue(operation["original_sequence"]["fresh_session"])
            self.assertTrue(operation["reduction_steps"])
            self.assertTrue(all(step["reason"] for step in operation["reduction_steps"]))
            self.assertEqual(operation["confirmation"]["runs"], 5)
            self.assertEqual(operation["confirmation"]["hits"], 5)
            self.assertTrue(next(out.glob("crash*#8.original.bin")).exists())
            self.assertTrue(next(out.glob("crash*#9.original.bin")).exists())

    def test_multi_session_sequence_reduction_preserves_required_state(self) -> None:
        import tempfile

        case = SimpleMessageGenerator(20260929).generate(11)
        first = run_isolated(__import__("targets.simple_message", fromlist=["replay_session"]).replay_session,
                             case.sequence, 1.0)
        self.assertIs(first.outcome, Outcome.CRASH)
        self.assertEqual(first.fault_function, "_commit_armed_operation")
        report = reduce_case(
            case.sequence,
            first.signature,
            lambda sequence: run_isolated(
                __import__("targets.simple_message", fromlist=["replay_session"]).replay_session,
                sequence, 1.0),
            repeats=1,
        )
        self.assertEqual(report.minimized.interaction_count, 2)
        self.assertEqual(report.minimized.steps[0].label, "enable-guarded-channel-state")
        self.assertEqual(report.minimized.steps[1].label, "guarded-channel-trigger")
        required = {(item["interaction"], item["label"]) for item in report.critical_positions
                    if item["type"] == "interaction"}
        self.assertIn((0, "enable-guarded-channel-state"), required)
        self.assertIn((1, "guarded-channel-trigger"), required)
        labels = [step.label for step in report.minimized.steps]
        self.assertNotIn("removable-benign-interaction", labels)
        with tempfile.NamedTemporaryFile("w", suffix=".session.json", delete=False) as handle:
            handle.write(report.minimized.to_json())
            replay_path = handle.name
        import json
        from pathlib import Path
        replayed = SessionSequence.from_dict(json.loads(Path(replay_path).read_text(encoding="utf-8")))
        self.assertEqual(replayed, report.minimized)

    def test_unfinished_reduction_resumes_without_new_budget(self) -> None:
        import json
        import tempfile
        from pathlib import Path

        from targets.simple_message import replay_session

        case = SimpleMessageGenerator(20260929).generate(11)
        first = run_isolated(replay_session, case.sequence, 1.0)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            finding = Finding(
                first.signature,
                case.sequence,
                case.sequence,
                11,
                0,
                stable=True,
                reduction_completed=False,
            )
            finding.save(out)
            (out / "campaign.checkpoint.json").write_text(json.dumps({
                "seed": 20260929,
                "target": "targets.simple_message.replay_session",
                "next_index": 11,
                "cases_executed": 11,
                "findings": [{
                    "key": first.signature.key(),
                    "original_index": 11,
                    "file": f"{finding.file_stem}.json",
                    "stable": True,
                    "reduction_completed": False,
                }],
            }), encoding="utf-8")
            campaign = FuzzingCampaign(
                "targets.simple_message.replay_session",
                Budget(max_cases=0, timeout=1.0),
                out,
                20260929,
            )
            summary = campaign.run()
            self.assertTrue(summary.resumed)
            self.assertEqual(summary.cases_executed, 11)
            payload = json.loads((out / f"{finding.file_stem}.json").read_text(encoding="utf-8"))
            self.assertTrue(payload["reduction_completed"])
            self.assertEqual(payload["minimized_sequence"]["interaction_count"], 2)
            self.assertTrue(payload["reduction_steps"])


if __name__ == "__main__":
    unittest.main()
