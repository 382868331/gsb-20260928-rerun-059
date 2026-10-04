from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from proto_robust import preparation
from proto_robust.differential import DifferentialCondition, DifferenceKind, VersionSpec
from proto_robust.models import Budget, Outcome
from proto_robust.paired_groups import PairedGroupCampaign
from proto_robust.session_campaign import signature_from_result
from proto_robust.session_runner import replay_sequence_isolated
from proto_robust.sequences import Interaction, SessionSequence

OLD = VersionSpec("old", "targets.session_store_shared.replay",
                  "targets.session_store_shared.describe_state")
NEW = VersionSpec("new", "targets.session_store_shared_new.replay",
                  "targets.session_store_shared_new.describe_state")


def _sequence(with_seal: bool = False) -> SessionSequence:
    from targets.session_store import frame
    steps = [
        Interaction("open", frame(0x10)),
        Interaction("prepare-mode-1", frame(0x20, 1)),
        Interaction("select-bucket-2", frame(0x30, 2)),
    ]
    if with_seal:
        steps.append(Interaction("seal", frame(0xC0)))
    steps.append(Interaction("commit-overread", frame(0x50, 3)))
    return SessionSequence(0, "shared-journal", "closed", tuple(steps))


def _condition() -> DifferentialCondition:
    import os
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["GSB_SHARED_STATE_DIR"] = tmp
        try:
            old_result = replay_sequence_isolated(
                _sequence(), OLD.replay_name, OLD.describe_name, 2.0,
                attempt_namespace="setup-old",
            )
        finally:
            os.environ.pop("GSB_SHARED_STATE_DIR", None)
    assert old_result.outcome == Outcome.CRASH.value, old_result.to_dict()
    return DifferentialCondition(
        DifferenceKind.OLD_ONLY, OLD, NEW, signature_from_result(old_result),
    )


class PreparationSnapshotTests(unittest.TestCase):
    def test_verify_requires_content_match_not_directory_existence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "journal.log").write_bytes(b"GENESIS\n")
            snapshot = Path(tmp) / "snapshot"
            manifest = preparation.freeze(source, snapshot)
            self.assertTrue(manifest["manifest_sha256"])

            target = Path(tmp) / "target"
            target.mkdir()
            # Directory exists and even the file exists, but content differs.
            (target / "journal.log").write_bytes(b"POLLUTED\n")
            evidence = preparation.verify(snapshot, target)
            self.assertFalse(evidence["ok"])
            self.assertEqual(evidence["mismatched"], ["journal.log"])
            checked = evidence["checked"][0]
            self.assertEqual(checked["expected_sha256"], manifest["files"][0]["sha256"])
            self.assertNotEqual(checked["observed_sha256"], checked["expected_sha256"])

            restored = preparation.restore(snapshot, target)
            self.assertTrue(restored["ok"])
            self.assertEqual((target / "journal.log").read_bytes(), b"GENESIS\n")

    def test_restore_refuses_unremovable_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "journal.log").write_bytes(b"GENESIS\n")
            snapshot = Path(tmp) / "snapshot"
            preparation.freeze(source, snapshot)
            target = Path(tmp) / "target"
            target.mkdir()
            (target / "artifact.lock").write_text(preparation.UNREMOVABLE_MARKER,
                                                 encoding="utf-8")
            with self.assertRaises(preparation.PreparationRestoreError):
                preparation.restore(snapshot, target)


class PairedGroupCampaignTests(unittest.TestCase):
    def test_restore_keeps_statistics_identical_across_orders(self) -> None:
        condition = _condition()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "restored"
            summary = PairedGroupCampaign(
                _sequence(), condition, Budget(16, 2.0), out, 20261004,
                groups=2, pairs_per_group=2, order_mode="alternating",
            ).run()
            self.assertEqual(summary.groups_completed, 2)
            payload = json.loads((out / "paired.json").read_text(encoding="utf-8"))
            outcomes = payload["outcomes"]
            # Both execution orders hit: save/restore makes the statistics
            # independent of the order the sides ran in.
            self.assertEqual(outcomes["hit"], 4)
            self.assertEqual(outcomes["valid"], 4)
            self.assertEqual(outcomes["preparation_failed"], 0)
            self.assertEqual(payload["order_conclusion"]["verdict"], "both_orders")
            orders = payload["order_conclusion"]["orders"]
            self.assertEqual(orders["old->new"]["hit"], 2)
            self.assertEqual(orders["new->old"]["hit"], 2)
            # Every side carries hash-level preparation evidence.
            checkpoint = json.loads(
                (out / "paired.checkpoint.json").read_text(encoding="utf-8"))
            for record in checkpoint["records"]:
                self.assertEqual(set(record["preparation"]), {"old", "new"})
                for evidence in record["preparation"].values():
                    self.assertTrue(evidence["ok"])
                    self.assertEqual(
                        evidence["manifest_sha256"],
                        record["snapshot_manifest_sha256"],
                    )
                    self.assertTrue(evidence["checked"][0]["observed_sha256"])

    def test_without_restore_old_first_pollutes_new_side(self) -> None:
        condition = _condition()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "polluted"
            PairedGroupCampaign(
                _sequence(), condition, Budget(16, 2.0), out, 20261004,
                groups=2, pairs_per_group=2, order_mode="alternating",
                restore_enabled=False,
            ).run()
            payload = json.loads((out / "paired.json").read_text(encoding="utf-8"))
            orders = payload["order_conclusion"]["orders"]
            # old->new: the old run's journal writes break the new side.
            self.assertEqual(orders["old->new"]["hit"], 0)
            self.assertEqual(orders["old->new"]["rejected"], 2)
            # new->old: the new side runs from the pristine journal.
            self.assertEqual(orders["new->old"]["hit"], 2)
            self.assertEqual(
                payload["order_conclusion"]["verdict"], "order_dependent:new->old")

    def test_unrecoverable_restore_keeps_failure_position_and_executed_side(self) -> None:
        condition = _condition()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "sealed"
            summary = PairedGroupCampaign(
                _sequence(with_seal=True), condition, Budget(16, 2.0), out, 20261004,
                groups=1, pairs_per_group=2, order_mode="fixed",
            ).run()
            self.assertEqual(summary.groups_completed, 1)
            payload = json.loads((out / "paired.json").read_text(encoding="utf-8"))
            outcomes = payload["outcomes"]
            self.assertEqual(outcomes["preparation_failed"], 2)
            self.assertEqual(outcomes["valid"], 0)
            self.assertEqual(outcomes["hit"], 0)
            checkpoint = json.loads(
                (out / "paired.checkpoint.json").read_text(encoding="utf-8"))
            first, second = checkpoint["records"]
            # The old side ran and crashed; the failure position and the
            # executed part are kept, and no replacement pair was drawn.
            self.assertEqual(first["executed_sides"], ["old"])
            self.assertIn("restore:before-new-after-old", first["failure_position"])
            self.assertEqual(first["old"]["outcome"], Outcome.CRASH.value)
            self.assertEqual(first["new"], {})
            self.assertEqual(first["disposition"], "preparation_failed")
            # The unremovable artifact still blocks the next pair at its start.
            self.assertEqual(second["executed_sides"], [])
            self.assertIn("restore:pair-start", second["failure_position"])
            self.assertEqual(second["disposition"], "preparation_failed")
            self.assertEqual(len(checkpoint["records"]), 2)

    def test_resume_continues_from_first_unexecuted_group(self) -> None:
        condition = _condition()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "resume"
            first = PairedGroupCampaign(
                _sequence(), condition, Budget(2, 2.0), out, 20261004,
                groups=3, pairs_per_group=2, order_mode="alternating",
            ).run()
            self.assertTrue(first.budget_exhausted)
            self.assertEqual(first.pairs_executed, 2)
            self.assertEqual(first.groups_completed, 1)
            resumed = PairedGroupCampaign(
                _sequence(), condition, Budget(16, 2.0), out, 20261004,
                groups=3, pairs_per_group=2, order_mode="alternating",
            ).run()
            self.assertTrue(resumed.resumed)
            self.assertEqual(resumed.pairs_executed, 6)
            self.assertEqual(resumed.groups_completed, 3)
            payload = json.loads((out / "paired.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["groups_remaining"], 0)
            self.assertEqual(payload["outcomes"]["hit"], 6)
            executed = [group["group"] for group in payload["groups_detail"]
                        if group["executed"]]
            self.assertEqual(executed, [0, 1, 2])
            # Each group froze its own verifiable snapshot, reused on resume.
            for group in range(3):
                snapshot = out / "snapshots" / f"group-{group}"
                self.assertTrue((snapshot / preparation.MANIFEST_NAME).exists())
            checkpoint = json.loads(
                (out / "paired.checkpoint.json").read_text(encoding="utf-8"))
            manifests = {record["snapshot_manifest_sha256"]
                         for record in checkpoint["records"]}
            self.assertEqual(len(manifests), 1)


if __name__ == "__main__":
    unittest.main()
