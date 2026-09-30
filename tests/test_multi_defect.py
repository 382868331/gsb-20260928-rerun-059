from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from proto_robust.fuzzer import FuzzingCampaign
from proto_robust.models import (
    Budget,
    FailureSignature,
    Finding,
    Outcome,
    ReproEvidence,
    TargetResult,
)
from proto_robust.reducer import reduce_case, reduce_finding
from proto_robust.runner import IsolatedWorker, failure_condition, raise_site
from targets import multi_defect
from targets.multi_defect import parse_multi

SITE_A = "multi_defect.py:73"
SITE_B = "multi_defect.py:84"
SITE_C = "multi_defect.py:103"

CASE_A = b"SM\x01\x02\x00\x05\xfeAAAA"
CASE_B = b"SM\x01\x03\x00\x05\xfdBBBB"
CASE_C = b"SM\x01\x09\x00\x00"


def direct_run(data: bytes) -> TargetResult:
    try:
        parse_multi(bytes(data))
    except Exception as exc:
        name = type(exc).__name__
        outcome = Outcome.CRASH if name == "IndexError" else Outcome.REJECTED
        return TargetResult(
            outcome,
            0.0,
            name,
            str(exc),
            site=raise_site(exc, "targets.multi_defect"),
            condition=failure_condition(name, str(exc)),
        )
    return TargetResult(Outcome.OK, 0.0)


class SameExceptionGroupingTests(unittest.TestCase):
    def test_same_exception_at_different_sites_has_distinct_keys(self) -> None:
        worker = IsolatedWorker("targets.multi_defect.parse_multi")
        try:
            result_a = worker.run(b"SM\x01\x02\x00\x01\xfe", 1.0)
            result_b = worker.run(b"SM\x01\x03\x00\x01\xfd", 1.0)
        finally:
            worker.close()
        self.assertEqual(result_a.exception_type, "IndexError")
        self.assertEqual(result_b.exception_type, "IndexError")
        self.assertNotEqual(result_a.site, result_b.site)
        self.assertNotEqual(result_a.signature.key(), result_b.signature.key())
        self.assertIn(SITE_A, result_a.signature.key())
        self.assertIn(SITE_B, result_b.signature.key())

    def test_reduction_keeps_original_site_not_just_exception_type(self) -> None:
        signature = FailureSignature(Outcome.CRASH, "IndexError", "", SITE_A, "index:4")
        minimized, _, critical = reduce_case(CASE_A, signature, direct_run)
        self.assertEqual(minimized, b"SM\x01\x02\x00\x01\xfe")
        self.assertIn(6, critical)
        self.assertEqual(direct_run(minimized).site, SITE_A)

    def test_reduction_of_b_stays_at_b(self) -> None:
        signature = FailureSignature(Outcome.CRASH, "IndexError", "", SITE_B, "index:2")
        minimized, _, _ = reduce_case(CASE_B, signature, direct_run)
        self.assertEqual(minimized, b"SM\x01\x03\x00\x01\xfd")
        self.assertEqual(direct_run(minimized).site, SITE_B)


class FlakyTimingTests(unittest.TestCase):
    def setUp(self) -> None:
        multi_defect.reset_state()

    def test_timing_defect_is_intermittent_across_repeated_trials(self) -> None:
        worker = IsolatedWorker("targets.multi_defect.parse_multi")
        try:
            outcomes = [worker.run(CASE_C, 1.0).outcome for _ in range(4)]
        finally:
            worker.close()
        self.assertIn(Outcome.CRASH, outcomes)
        self.assertIn(Outcome.OK, outcomes)

    def test_reduction_keeps_flaky_sample_with_evidence(self) -> None:
        signature = FailureSignature(Outcome.CRASH, "IndexError", "", SITE_C, "index:1")
        original = b"SM\x01\x09\x00\x05\x00\x00\x00\x00\x00"
        finding = Finding(
            signature,
            original,
            original,
            12,
            0,
            repro=ReproEvidence(2, 4, ["crash", "ok", "crash", "ok"]),
        )

        def flaky_run(data: bytes) -> TargetResult:
            try:
                parse_multi(bytes(data))
            except IndexError:
                return TargetResult(Outcome.CRASH, 0.0, "IndexError", "timing state index 1 out of range", site=SITE_C, condition="index:1")
            except Exception:
                return TargetResult(Outcome.REJECTED, 0.0, "ParseReject", "", site="")
            return TargetResult(Outcome.OK, 0.0)

        reduce_finding(
            finding,
            flaky_run,
            flaky=True,
            confirm_trials=4,
            threshold=0.5,
        )
        self.assertTrue(finding.reduction_done)
        self.assertEqual(finding.minimized[3], 9)
        self.assertIn(3, finding.critical_byte_offsets)
        self.assertTrue(finding.reduction_steps)
        for step in finding.reduction_steps:
            self.assertTrue(step.reason)
            self.assertEqual(step.trials, 4)


class BudgetAndResumeTests(unittest.TestCase):
    def test_campaign_tracks_all_executions_and_exhausts_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "run"
            campaign = FuzzingCampaign(
                "targets.multi_defect.parse_multi",
                Budget(max_cases=12, timeout=1.0, max_executions=10),
                output,
                20260929,
                confirm_trials=4,
            )
            summary = campaign.run()
            self.assertEqual(summary.status, "budget_exhausted")
            self.assertTrue((output / "campaign.json").exists())

    def test_resume_preserves_originals_reasons_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "run"
            tiny = FuzzingCampaign(
                "targets.multi_defect.parse_multi",
                Budget(max_cases=12, timeout=1.0, max_executions=13),
                output,
                20260929,
                confirm_trials=4,
            )
            first = tiny.run()
            self.assertEqual(first.status, "budget_exhausted")

            originals = sorted(p.read_bytes() for p in output.glob("*.original.bin"))
            self.assertTrue(originals)
            step_count_before = {}
            for path in output.glob("*.json"):
                if path.name == "campaign.json":
                    continue
                payload = json.loads(path.read_text(encoding="utf-8"))
                step_count_before[path.name] = len(payload["reduction_steps"])

            resumed = FuzzingCampaign(
                "targets.multi_defect.parse_multi",
                Budget(max_cases=64, timeout=1.0, max_executions=2000),
                output,
                20260929,
                confirm_trials=4,
            )
            second = resumed.run(resume=True)
            self.assertEqual(second.status, "completed")
            keys = {f.signature.key() for f in resumed.findings}
            self.assertTrue(any(SITE_A in k and "index:4" in k for k in keys), keys)
            self.assertTrue(any(SITE_B in k and "index:2" in k for k in keys), keys)

            after = sorted(p.read_bytes() for p in output.glob("*.original.bin"))
            # Every original sample captured before resume stays byte-identical.
            for original in originals:
                self.assertIn(original, after)
            self.assertGreaterEqual(len(after), len(originals))
            done = [f for f in resumed.findings if f.reduction_done]
            self.assertGreaterEqual(len(done), 2)
            for path in output.glob("*.json"):
                if path.name == "campaign.json":
                    continue
                payload = json.loads(path.read_text(encoding="utf-8"))
                reasons = [s["reason"] for s in payload["reduction_steps"]]
                self.assertTrue(reasons)
                if path.name in step_count_before:
                    self.assertGreaterEqual(len(reasons), step_count_before[path.name])
                self.assertIn("repro", payload)
                self.assertIn(payload["signature"]["site"], payload["signature"]["key"])


if __name__ == "__main__":
    unittest.main()
