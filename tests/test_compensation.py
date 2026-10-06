from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from proto_robust.compensation import (
    compare_schemes,
    find_gaps,
    plan_compensation,
)
from proto_robust.differential import VersionSpec
from proto_robust.models import Budget
from proto_robust.paired_campaign import PairedReplayCampaign, load_paired_goal

OLD = VersionSpec("old", "targets.shared_file_store.replay",
                  "targets.shared_file_store.describe_state")
NEW = VersionSpec("new", "targets.shared_file_store_new.replay",
                  "targets.shared_file_store.describe_state")

TIMEOUT = 0.5


def timing_groups(pairs_a: int = 2, pairs_b: int = 2):
    base = load_paired_goal("goals/shared_file_pairs.json")[0][2]
    return [
        dataclasses.replace(base, group_id="timing-a", pairs=pairs_a),
        dataclasses.replace(base, group_id="timing-b", pairs=pairs_b),
    ]


def fake_gap_records(plan, disposition="timeout"):
    return [{
        "group": entry["group"],
        "pair": entry["pair"],
        "planned_order": list(entry["planned_order"]),
        "disposition": disposition,
        "executed": [],
        "sides": {},
        "failure_location": "",
    } for entry in plan]


class CompensationPlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "paired"

    def make_campaign(self, groups) -> PairedReplayCampaign:
        return PairedReplayCampaign(
            groups, OLD, NEW, Budget(64, TIMEOUT), self.out, 20261006, fresh=True,
        )

    def test_fill_failed_appends_replacements_with_provenance(self):
        campaign = self.make_campaign(timing_groups())
        campaign.records = fake_gap_records(campaign.plan)
        report = campaign.apply_compensation("fill_failed")
        self.assertEqual(report["status"], "ok")
        self.assertEqual(len(report["additions"]), 4)
        self.assertEqual(len(campaign.plan), 8)
        additions = campaign.plan[4:]
        for addition, gap in zip(additions, report["gaps"]):
            pass
        by_gap = {}
        for addition in additions:
            key = (addition["compensates"]["group"], addition["compensates"]["pair"])
            by_gap.setdefault(key, []).append(addition)
        self.assertEqual(len(by_gap), 4)
        for (group, pair), entries in by_gap.items():
            self.assertEqual(len(entries), 1)
            original = campaign._plan_entry(group, pair)
            self.assertEqual(entries[0]["planned_order"], original["planned_order"])
            self.assertEqual(entries[0]["group"], group)
        # The appended tail never places the same object twice in a row,
        # including the boundary with the existing sequence.
        tail = [campaign.plan[3]["group"]] + [entry["group"] for entry in additions]
        for first, second in zip(tail, tail[1:]):
            self.assertNotEqual(first, second)

    def test_repeated_request_never_appends_twice(self):
        campaign = self.make_campaign(timing_groups())
        campaign.records = fake_gap_records(campaign.plan)
        first = campaign.apply_compensation("fill_failed")
        self.assertEqual(len(first["additions"]), 4)
        second = campaign.apply_compensation("fill_failed")
        self.assertEqual(second["status"], "ok")
        self.assertEqual(second["additions"], [])
        self.assertEqual(len(second["already_compensated"]), 4)
        self.assertEqual(len(campaign.plan), 8)

    def test_keep_gaps_appends_nothing(self):
        campaign = self.make_campaign(timing_groups())
        campaign.records = fake_gap_records(campaign.plan)
        report = campaign.apply_compensation("keep_gaps")
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["additions"], [])
        self.assertEqual(len(report["gaps"]), 4)
        self.assertEqual(len(campaign.plan), 4)

    def test_append_balanced_rebalances_gap_groups(self):
        campaign = self.make_campaign(timing_groups(pairs_a=3, pairs_b=3))
        campaign.records = fake_gap_records(campaign.plan)
        report = campaign.apply_compensation("append_balanced")
        self.assertEqual(report["status"], "ok")
        reasons = [addition["reason"] for addition in report["additions"]]
        self.assertIn("append_balanced", reasons)
        metrics = campaign.scheme_comparison()["append_balanced"]["metrics"]
        self.assertEqual(metrics["total_order_imbalance"], 0)
        tail = [campaign.plan[5]["group"]] + [entry["group"] for entry in campaign.plan[6:]]
        for first, second in zip(tail, tail[1:]):
            self.assertNotEqual(first, second)
        # Every appended position keeps the gap it came from.
        for entry in campaign.plan[6:]:
            self.assertIn("compensates", entry)
            self.assertEqual(entry["compensates"]["group"], entry["group"])

    def test_infeasible_scheme_reports_conflicting_constraints(self):
        groups = timing_groups(pairs_a=2, pairs_b=2)[:1]
        campaign = self.make_campaign(groups)
        campaign.records = fake_gap_records(campaign.plan)
        plan_before = [dict(entry) for entry in campaign.plan]
        report = campaign.apply_compensation("fill_failed")
        self.assertEqual(report["status"], "infeasible")
        names = {conflict["constraint"] for conflict in report["conflicts"]}
        self.assertIn("no_consecutive_same_object", names)
        self.assertIn("single_shared_execution_resource", names)
        self.assertIn("executed_order_immutable", names)
        # The plan is untouched: no positions appended, gaps not erased.
        self.assertEqual(len(campaign.plan), len(plan_before))
        self.assertEqual(len(find_gaps(campaign.plan, campaign.records)), 2)

    def test_scheme_comparison_covers_all_strategies(self):
        campaign = self.make_campaign(timing_groups())
        campaign.records = fake_gap_records(campaign.plan)
        # One pair stayed valid; the rest dropped out.
        campaign.records[2]["disposition"] = "hit"
        comparison = compare_schemes(campaign.plan, campaign.records)
        self.assertEqual(
            set(comparison), {"keep_gaps", "fill_failed", "append_balanced"})
        keep = comparison["keep_gaps"]["metrics"]
        fill = comparison["fill_failed"]["metrics"]
        self.assertEqual(keep["gaps"], 3)
        self.assertGreater(keep["total_order_imbalance"], 0)
        self.assertLess(keep["combinations_covered"], keep["combinations_total"])
        self.assertEqual(fill["total_order_imbalance"], 0)
        self.assertGreater(fill["positions"], keep["positions"])
        self.assertEqual(
            fill["combinations_covered"], fill["combinations_total"])


class CompensationExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "paired"

    def test_compensated_plan_pauses_and_resumes_in_order(self):
        groups = timing_groups()
        first = PairedReplayCampaign(
            groups, OLD, NEW, Budget(4, TIMEOUT), self.out, 20261006, fresh=True,
        )
        summary = first.run()
        self.assertEqual(summary.pairs_executed, 4)
        self.assertEqual(summary.totals["timeout"], 4)

        report = first.apply_compensation("fill_failed")
        self.assertEqual(report["status"], "ok")
        self.assertEqual(len(first.plan), 8)

        # Pause after only part of the compensation tail.
        second = PairedReplayCampaign(
            groups, OLD, NEW, Budget(6, TIMEOUT), self.out, 20261006,
        )
        paused = second.run()
        self.assertTrue(paused.resumed)
        self.assertEqual(paused.pairs_executed, 6)
        checkpoint = json.loads(
            (self.out / "paired.checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(len(checkpoint["plan"]), 8)
        self.assertIsNotNone(checkpoint["next"])

        # Resume to completion: execution order follows the saved plan.
        third = PairedReplayCampaign(
            groups, OLD, NEW, Budget(8, TIMEOUT), self.out, 20261006,
        )
        done = third.run()
        self.assertEqual(done.pairs_executed, 8)
        payload = json.loads((self.out / "paired.json").read_text(encoding="utf-8"))
        keys = [(r["group"], r["pair"]) for r in payload["records"]]
        self.assertEqual(len(keys), len(set(keys)))
        planned_keys = [(e["group"], e["pair"]) for e in checkpoint["plan"]]
        self.assertEqual(keys, planned_keys)

        # Failed placeholders remain; replacements point back at their gap.
        originals = [r for r in payload["records"] if "compensates" not in r]
        replacements = [r for r in payload["records"] if "compensates" in r]
        self.assertEqual(len(originals), 4)
        self.assertTrue(all(r["disposition"] == "timeout" for r in originals))
        self.assertEqual(len(replacements), 4)
        for replacement in replacements:
            gap = replacement["compensates"]
            original = next(
                r for r in originals
                if r["group"] == gap["group"] and r["pair"] == gap["pair"])
            self.assertEqual(replacement["planned_order"], original["planned_order"])
        self.assertEqual(payload["missing_pairs"], [])
        self.assertIsNotNone(payload["compensation"])
        self.assertEqual(payload["compensation"]["strategy"], "fill_failed")
        self.assertIn("scheme_comparison", payload)

        # Re-applying after completion still does not duplicate.
        again = third.apply_compensation("fill_failed")
        self.assertEqual(again["additions"], [])
        self.assertEqual(len(third.plan), 8)


if __name__ == "__main__":
    unittest.main()
