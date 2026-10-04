from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from proto_robust.differential import VersionSpec
from proto_robust.models import Budget
from proto_robust.paired_campaign import (
    PairedGroupSpec,
    PairedReplayCampaign,
    load_paired_goal,
)
from proto_robust.preparation import (
    freeze_preparation,
    restore_preparation,
    verify_preparation,
)

OLD = VersionSpec("old", "targets.shared_file_store.replay",
                  "targets.shared_file_store.describe_state")
NEW = VersionSpec("new", "targets.shared_file_store_new.replay",
                  "targets.shared_file_store.describe_state")

TIMEOUT = 1.5


def goal_groups() -> list[PairedGroupSpec]:
    groups, _payload = load_paired_goal("goals/shared_file_pairs.json")
    return groups


class PreparationSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.snapshot = self.root / "snapshot"
        freeze_preparation("targets/shared_state/ledger_clean", self.snapshot)

    def test_restore_is_verified_by_content_hash(self) -> None:
        target = self.root / "runtime"
        report = restore_preparation(self.snapshot, target)
        self.assertTrue(report.verified)
        self.assertEqual(report.checked_files, 2)
        self.assertEqual(report.mismatches, [])

    def test_tampered_content_is_detected_with_location(self) -> None:
        target = self.root / "runtime"
        restore_preparation(self.snapshot, target)
        (target / "ledger.log").write_text("seed-0\nseed-1\nextra\n", encoding="utf-8")
        report = verify_preparation(
            json.loads((self.snapshot / "manifest.json").read_text(encoding="utf-8")),
            target,
        )
        self.assertFalse(report.verified)
        self.assertEqual(report.failure_location, "ledger.log")
        self.assertEqual(report.mismatches[0]["path"], "ledger.log")
        self.assertNotEqual(report.mismatches[0]["expected"],
                            report.mismatches[0]["actual"])

    def test_missing_and_extra_files_are_detected(self) -> None:
        target = self.root / "runtime"
        restore_preparation(self.snapshot, target)
        (target / "slots.json").unlink()
        (target / "stray.tmp").write_text("x", encoding="utf-8")
        report = verify_preparation(
            json.loads((self.snapshot / "manifest.json").read_text(encoding="utf-8")),
            target,
        )
        self.assertFalse(report.verified)
        self.assertEqual(report.missing, ["slots.json"])
        self.assertEqual(report.extra, ["stray.tmp"])

    def test_restore_rebuilds_a_polluted_directory(self) -> None:
        target = self.root / "runtime"
        restore_preparation(self.snapshot, target)
        with (target / "ledger.log").open("a", encoding="utf-8") as handle:
            handle.write("PUT:archive\n")
        report = restore_preparation(self.snapshot, target)
        self.assertTrue(report.verified)
        self.assertEqual(
            (target / "ledger.log").read_text(encoding="utf-8"),
            (self.snapshot / "files" / "ledger.log").read_text(encoding="utf-8"),
        )


class PairedReplayCampaignTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "paired"

    def run_campaign(self, cases: int = 6, restore: bool = True,
                     fresh: bool = False, out: Path | None = None):
        campaign = PairedReplayCampaign(
            goal_groups(), OLD, NEW, Budget(cases, TIMEOUT),
            out or self.out, 20261004,
            restore_between_sides=restore, fresh=fresh,
        )
        return campaign.run()

    def test_restore_keeps_statistics_identical_across_orders(self) -> None:
        summary = self.run_campaign()
        self.assertFalse(summary.budget_exhausted)
        payload = json.loads((self.out / "paired.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["totals"]["valid"], 4)
        self.assertEqual(payload["totals"]["hits"], 4)
        self.assertEqual(payload["totals"]["timeout"], 2)
        self.assertEqual(payload["totals"]["preparation_failed"], 0)
        orders = payload["orders"]
        self.assertEqual(orders["old->new"], orders["new->old"])
        for record in payload["records"]:
            for side in record["sides"].values():
                preparation = side["preparation"]
                self.assertTrue(preparation["verified"])
                self.assertGreaterEqual(preparation["checked_files"], 2)

    def test_without_restore_one_order_pollutes_the_other_side(self) -> None:
        self.run_campaign(restore=False)
        payload = json.loads((self.out / "paired.json").read_text(encoding="utf-8"))
        orders = payload["orders"]
        self.assertEqual(orders["old->new"]["misses"], 1)
        self.assertEqual(orders["new->old"]["misses"], 0)
        polluted = next(
            record for record in payload["records"]
            if record["group"] == "ledger-pollution"
            and record["planned_order"] == ["old", "new"]
        )
        new_side = polluted["sides"]["new"]
        self.assertIsNone(new_side["preparation"])
        self.assertEqual(new_side["result"]["outcome"], "crash")
        self.assertEqual(new_side["result"]["exception_type"], "IndexError")
        self.assertEqual(new_side["result"]["fault_function"], "_select_bucket")

    def test_resume_continues_from_unexecuted_groups(self) -> None:
        first = self.run_campaign(cases=3, fresh=True)
        self.assertTrue(first.budget_exhausted)
        self.assertEqual(first.pairs_executed, 3)
        checkpoint = json.loads((self.out / "paired.checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["next"], {"group": "commit-table", "pair": 1})
        resumed = self.run_campaign(cases=6)
        self.assertTrue(resumed.resumed)
        self.assertEqual(resumed.pairs_executed, 6)
        payload = json.loads((self.out / "paired.json").read_text(encoding="utf-8"))
        keys = [(r["group"], r["pair"]) for r in payload["records"]]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(payload["totals"]["timeout"], 2)
        self.assertEqual(payload["totals"]["hits"], 4)

    def test_preparation_failure_keeps_partial_pair_without_resampling(self) -> None:
        from proto_robust import paired_campaign
        from proto_robust.preparation import RestoreReport

        real_restore = paired_campaign.restore_preparation
        calls = {"n": 0}

        def flaky_restore(snapshot_dir, target_dir):
            calls["n"] += 1
            if calls["n"] == 2:
                return RestoreReport(
                    verified=False,
                    checked_files=2,
                    mismatches=[{"path": "ledger.log", "expected": "a", "actual": "b"}],
                    failure_location="ledger.log",
                )
            return real_restore(snapshot_dir, target_dir)

        groups = goal_groups()[:1]
        with mock.patch.object(paired_campaign, "restore_preparation", flaky_restore):
            campaign = PairedReplayCampaign(
                groups, OLD, NEW, Budget(4, TIMEOUT), self.out, 20261004,
            )
            summary = campaign.run()
        self.assertEqual(summary.pairs_executed, 2)
        payload = json.loads((self.out / "paired.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["totals"]["preparation_failed"], 1)
        self.assertEqual(payload["totals"]["valid"], 1)
        failed = next(r for r in payload["records"] if r["disposition"] == "preparation_failed")
        self.assertEqual(failed["failure_location"], "ledger.log")
        self.assertEqual(failed["executed"], ["old"])
        self.assertEqual(failed["sides"]["old"]["result"]["outcome"], "ok")
        self.assertNotIn("result", failed["sides"]["new"])
        self.assertFalse(failed["sides"]["new"]["preparation"]["verified"])
        # The failed pair was recorded once and never re-sampled.
        self.assertEqual(len(payload["records"]), 2)

    def test_groups_reported_separately(self) -> None:
        self.run_campaign()
        payload = json.loads((self.out / "paired.json").read_text(encoding="utf-8"))
        by_id = {group["group"]: group for group in payload["groups"]}
        self.assertEqual(by_id["ledger-pollution"]["hits"], 2)
        self.assertEqual(by_id["commit-table"]["hits"], 2)
        self.assertEqual(by_id["timing-stall"]["timeout"], 2)
        self.assertEqual(by_id["timing-stall"]["valid"], 0)


class OrderBalancePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "paired"

    @staticmethod
    def two_groups_three_pairs() -> list[PairedGroupSpec]:
        import dataclasses
        return [dataclasses.replace(group, pairs=3) for group in goal_groups()[:2]]

    def run_campaign(self, groups, cases, balance="per-group", fresh=False):
        campaign = PairedReplayCampaign(
            groups, OLD, NEW, Budget(cases, TIMEOUT), self.out, 20261005,
            order_balance=balance, fresh=fresh,
        )
        campaign.ensure_plan()
        planned = campaign.planned_order_counts()
        summary = campaign.run()
        return planned, summary

    def test_per_group_plan_counts_with_odd_pairs(self) -> None:
        groups = self.two_groups_three_pairs()
        planned, summary = self.run_campaign(groups, 6)
        self.assertEqual(planned, {"old->new": 4, "new->old": 2})
        payload = json.loads((self.out / "paired.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["order_balance"], "per-group")
        self.assertEqual(payload["order_plan"], planned)
        self.assertEqual(payload["missing_pairs"], [])
        executed = {key: totals["pairs"] for key, totals in payload["orders"].items()}
        self.assertEqual(executed, planned)

    def test_cross_group_plan_continues_alternation(self) -> None:
        groups = self.two_groups_three_pairs()
        planned, summary = self.run_campaign(groups, 6, balance="cross-group")
        self.assertEqual(planned, {"old->new": 3, "new->old": 3})
        payload = json.loads((self.out / "paired.json").read_text(encoding="utf-8"))
        second_group = [r for r in payload["records"] if r["group"] == groups[1].group_id]
        # The first group consumed an odd number of slots, so the second
        # group starts with new->old instead of restarting at old->new.
        self.assertEqual(second_group[0]["planned_order"], ["new", "old"])

    def test_interrupted_run_plan_executed_and_missing_stay_consistent(self) -> None:
        groups = self.two_groups_three_pairs()
        planned, first = self.run_campaign(groups, 4, fresh=True)
        self.assertTrue(first.budget_exhausted)
        self.assertEqual(first.pairs_executed, 4)
        payload = json.loads((self.out / "paired.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["order_plan"], {"old->new": 4, "new->old": 2})
        executed = {key: totals["pairs"] for key, totals in payload["orders"].items()}
        self.assertEqual(executed, {"old->new": 3, "new->old": 1})
        missing = payload["missing_pairs"]
        self.assertEqual(len(missing), 2)
        self.assertEqual(
            [(m["group"], m["pair"]) for m in missing],
            [(groups[1].group_id, 1), (groups[1].group_id, 2)],
        )
        self.assertEqual(missing[0]["planned_order"], ["new", "old"])
        self.assertEqual(missing[1]["planned_order"], ["old", "new"])
        self.assertTrue(all(m["reason"] == "budget_exhausted" for m in missing))
        # Executed plus missing accounts for every planned order slot.
        for key in planned:
            done = executed.get(key, 0)
            pending = sum(1 for m in missing if "->".join(m["planned_order"]) == key)
            self.assertEqual(done + pending, planned[key])

        # Resume with a *different* balance flag: the persisted plan wins
        # and the alternation does not restart from old->new.
        _planned, resumed = self.run_campaign(groups, 6, balance="cross-group")
        self.assertTrue(resumed.resumed)
        self.assertEqual(resumed.pairs_executed, 6)
        payload = json.loads((self.out / "paired.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["order_balance"], "per-group")
        self.assertEqual(payload["order_plan"], {"old->new": 4, "new->old": 2})
        self.assertEqual(payload["missing_pairs"], [])
        keys = [(r["group"], r["pair"]) for r in payload["records"]]
        self.assertEqual(len(keys), len(set(keys)))
        second_group = [r for r in payload["records"] if r["group"] == groups[1].group_id]
        self.assertEqual(
            [r["planned_order"] for r in second_group],
            [["old", "new"], ["new", "old"], ["old", "new"]],
        )
        executed = {key: totals["pairs"] for key, totals in payload["orders"].items()}
        self.assertEqual(executed, payload["order_plan"])


if __name__ == "__main__":
    unittest.main()
