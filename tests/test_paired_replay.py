from __future__ import annotations

import dataclasses
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


class OrderStrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "paired"

    @staticmethod
    def two_groups_three_pairs() -> list[PairedGroupSpec]:
        return [
            dataclasses.replace(group, pairs=3)
            for group in goal_groups()[:2]
        ]

    def run_campaign(self, cases: int, strategy: str, fresh: bool = False):
        campaign = PairedReplayCampaign(
            self.two_groups_three_pairs(), OLD, NEW, Budget(cases, TIMEOUT),
            self.out, 20261005, order_strategy=strategy, fresh=fresh,
        )
        return campaign.run()

    def summary_payload(self) -> dict:
        return json.loads((self.out / "paired.json").read_text(encoding="utf-8"))

    def test_per_group_odd_pairs_gives_each_group_one_extra_old_first(self) -> None:
        self.run_campaign(cases=6, strategy="per_group", fresh=True)
        payload = self.summary_payload()
        self.assertEqual(payload["order_strategy"], "per_group")
        self.assertEqual(payload["planned_orders"], {"old->new": 4, "new->old": 2})
        for group_id in ("ledger-pollution", "commit-table"):
            orders = [
                r["planned_order"] for r in payload["records"] if r["group"] == group_id
            ]
            self.assertEqual(orders, [["old", "new"], ["new", "old"], ["old", "new"]])
        self.assertEqual(payload["missing_pairs"], [])

    def test_cross_group_odd_pairs_stays_globally_balanced(self) -> None:
        self.run_campaign(cases=6, strategy="cross_group", fresh=True)
        payload = self.summary_payload()
        self.assertEqual(payload["planned_orders"], {"old->new": 3, "new->old": 3})
        orders = [r["planned_order"] for r in payload["records"]]
        self.assertEqual(
            orders,
            [["old", "new"], ["new", "old"]] * 3,
        )

    def test_resume_keeps_original_plan_and_counts_stay_consistent(self) -> None:
        first = self.run_campaign(cases=3, strategy="per_group", fresh=True)
        self.assertTrue(first.budget_exhausted)
        payload = self.summary_payload()
        self.assertEqual(payload["planned_orders"], {"old->new": 4, "new->old": 2})
        executed = {"old->new": 0, "new->old": 0}
        for record in payload["records"]:
            executed["->".join(record["planned_order"])] += 1
        missing = payload["missing_pairs"]
        self.assertEqual(len(missing), 3)
        self.assertTrue(all(item["reason"] == "budget_exhausted" for item in missing))
        missing_counts = {"old->new": 0, "new->old": 0}
        for item in missing:
            missing_counts["->".join(item["planned_order"])] += 1
        for key in ("old->new", "new->old"):
            self.assertEqual(
                payload["planned_orders"][key],
                executed[key] + missing_counts[key],
            )
        # Resume with the opposite strategy: the saved plan must win, so the
        # run must not re-alternate from the old version.
        resumed = self.run_campaign(cases=6, strategy="cross_group")
        self.assertTrue(resumed.resumed)
        self.assertEqual(resumed.pairs_executed, 6)
        payload = self.summary_payload()
        self.assertEqual(payload["order_strategy"], "per_group")
        self.assertEqual(payload["planned_orders"], {"old->new": 4, "new->old": 2})
        self.assertEqual(payload["missing_pairs"], [])
        keys = [(r["group"], r["pair"]) for r in payload["records"]]
        self.assertEqual(len(keys), len(set(keys)))
        for group_id in ("ledger-pollution", "commit-table"):
            orders = [
                r["planned_order"] for r in payload["records"] if r["group"] == group_id
            ]
            self.assertEqual(orders, [["old", "new"], ["new", "old"], ["old", "new"]])
        executed = {"old->new": 0, "new->old": 0}
        for record in payload["records"]:
            executed["->".join(record["planned_order"])] += 1
        self.assertEqual(executed, payload["planned_orders"])

    def test_timeout_occupies_planned_order_slot_without_resampling(self) -> None:
        groups = [
            dataclasses.replace(goal_groups()[2], pairs=3),
        ]
        campaign = PairedReplayCampaign(
            groups, OLD, NEW, Budget(3, TIMEOUT), self.out, 20261005,
            order_strategy="per_group", fresh=True,
        )
        campaign.run()
        payload = self.summary_payload()
        self.assertEqual(payload["totals"]["timeout"], 3)
        self.assertEqual(payload["missing_pairs"], [])
        orders = [r["planned_order"] for r in payload["records"]]
        self.assertEqual(orders, [["old", "new"], ["new", "old"], ["old", "new"]])
        self.assertEqual(payload["planned_orders"], {"old->new": 2, "new->old": 1})


class CompensationPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "paired"

    def campaign(self, groups, policy: str = "keep_gap", strategy: str = "per_group",
                 fresh: bool = True):
        return PairedReplayCampaign(
            groups, OLD, NEW, Budget(20, TIMEOUT), self.out, 20261006,
            order_strategy=strategy, compensation_policy=policy, fresh=fresh,
        )

    @staticmethod
    def fail_original(campaign, group_id: str, pair_index: int,
                      disposition: str = "withdrawn") -> None:
        planned = campaign._planned_order(group_id, pair_index)
        campaign.records.append({
            "slot_id": f"original:{group_id}:{pair_index}",
            "group": group_id,
            "pair": pair_index,
            "planned_order": planned,
            "slot_kind": "original",
            "source_gaps": [],
            "executed": [planned[0]],
            "sides": {planned[0]: {"result": {"outcome": "ok"}}},
            "disposition": disposition,
            "failure_location": "participant",
        })
        campaign.spent += 1

    def test_keep_gap_retains_failed_slot_and_compares_coverage(self) -> None:
        campaign = self.campaign([dataclasses.replace(goal_groups()[0], pairs=2)])
        self.fail_original(campaign, "ledger-pollution", 0)
        analysis = campaign.apply_compensation_policy()

        self.assertEqual(analysis["gaps"][0]["slot_id"], "original:ledger-pollution:0")
        self.assertEqual(analysis["schemes"]["keep_gap"]["retained_gaps"],
                         ["original:ledger-pollution:0"])
        self.assertEqual(len(campaign.plan), 2)
        self.assertEqual(campaign.records[0]["disposition"], "withdrawn")
        self.assertEqual(
            analysis["schemes"]["keep_gap"]["metrics"]["per_group_order_counts"],
            {"ledger-pollution": {"old->new": 0, "new->old": 1}},
        )

    def test_replace_failed_uses_only_matching_unexecuted_slot_and_records_source(self) -> None:
        groups = [dataclasses.replace(goal_groups()[0], pairs=4)]
        campaign = self.campaign(groups, policy="replace_failed")
        self.fail_original(campaign, "ledger-pollution", 0)
        analysis = campaign.apply_compensation_policy()

        reused = analysis["schemes"]["replace_failed"]["reused_unexecuted_slots"]
        self.assertEqual(len(reused), 1)
        self.assertEqual(reused[0]["slot_id"], "original:ledger-pollution:2")
        self.assertEqual(reused[0]["source_gap"], "original:ledger-pollution:0")
        replacement = next(
            entry for entry in campaign.plan if entry["slot_id"] == reused[0]["slot_id"]
        )
        self.assertEqual(replacement["source_gaps"], ["original:ledger-pollution:0"])
        self.assertEqual(len(campaign.plan), 4)

    def test_balanced_append_can_restore_even_effective_coverage(self) -> None:
        groups = [dataclasses.replace(goal_groups()[0], pairs=2)]
        campaign = self.campaign(groups, policy="balanced_append")
        self.fail_original(campaign, "ledger-pollution", 0)
        analysis = campaign.apply_compensation_policy()
        scheme = analysis["schemes"]["balanced_append"]
        self.assertEqual(scheme["conflicts"], [])
        added = scheme["additional_slots"]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["planned_order"], ["old", "new"])
        self.assertEqual(added[0]["source_gaps"], ["original:ledger-pollution:0"])

    def test_feasible_balanced_append_adds_explicitly_sourced_slot_once(self) -> None:
        groups = [dataclasses.replace(goal_groups()[0], pairs=4)]
        campaign = self.campaign(groups, policy="balanced_append")
        self.fail_original(campaign, "ledger-pollution", 0)
        analysis = campaign.apply_compensation_policy()
        self.assertEqual(analysis["schemes"]["balanced_append"]["conflicts"], [])
        added = [entry for entry in campaign.plan if entry["slot_kind"] == "compensation"]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["source_gaps"], ["original:ledger-pollution:0"])
        self.assertEqual(added[0]["planned_order"], ["old", "new"])
        campaign.apply_compensation_policy()
        self.assertEqual(
            len([entry for entry in campaign.plan if entry["slot_kind"] == "compensation"]),
            1,
        )

    def test_resume_freezes_policy_and_appended_execution_order(self) -> None:
        groups = [dataclasses.replace(goal_groups()[0], pairs=2)]
        campaign = self.campaign(groups, policy="balanced_append")
        self.fail_original(campaign, "ledger-pollution", 0)
        campaign.apply_compensation_policy()
        campaign._write_checkpoint()

        resumed = self.campaign(groups, policy="keep_gap", fresh=False)
        self.assertTrue(resumed.resumed)
        self.assertEqual(resumed.compensation_policy, "balanced_append")
        planned = [
            (entry["slot_id"], entry["planned_order"], entry.get("source_gaps", []))
            for entry in resumed._ordered_plan()
        ]
        self.assertEqual(planned[-1:], [
            ("compensation:0", ["old", "new"], ["original:ledger-pollution:0"]),
        ])

    def test_adjacent_repeat_conflict_is_reported_without_appending(self) -> None:
        groups = [dataclasses.replace(goal_groups()[0], pairs=3)]
        campaign = self.campaign(groups, policy="balanced_append", strategy="per_group")
        self.fail_original(campaign, "ledger-pollution", 1, disposition="timeout")
        analysis = campaign.apply_compensation_policy()
        scheme = analysis["schemes"]["balanced_append"]
        self.assertEqual(scheme["additional_slots"], [])
        self.assertTrue(scheme["retained_gaps"])
        self.assertTrue(scheme["additional_slots"] == [])
        self.assertTrue(any("adjacent" in item["constraint"] for item in scheme["conflicts"]))


if __name__ == "__main__":
    unittest.main()
