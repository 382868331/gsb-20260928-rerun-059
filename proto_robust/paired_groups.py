"""Paired replay over groups that share a frozen preparation snapshot.

Each group freezes one verifiable preparation snapshot of the shared-file
state; every pair in the group -- whatever its execution order -- starts
from that explicit, hash-checked state.  Restoration is proven by the
snapshot manifest (per-file sha256), never by the state directory merely
existing.

If one side ran and the preparation state cannot be restored afterwards,
the pair is recorded as ``preparation_failed`` with the failure position
and the actually executed parts kept as evidence.  The pair is left
incomplete on purpose: no favourable replacement sample is drawn to
"complete" it.

The checkpoint records the next unexecuted group/pair, so an interrupted
campaign resumes from the first unexecuted group without re-running or
double-counting finished pairs.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import time

from proto_robust import preparation
from proto_robust.differential import (
    DifferentialCondition,
    classify_pair,
    order_conclusion,
)
from proto_robust.models import Budget
from proto_robust.runner import load_callable
from proto_robust.session_campaign import _atomic_json
from proto_robust.session_runner import replay_sequence_isolated
from proto_robust.sequences import SessionSequence

CHECKPOINT_NAME = "paired.checkpoint.json"
SUMMARY_NAME = "paired.json"


@dataclass
class PairedGroupSummary:
    seed: int
    pairs_executed: int
    groups_completed: int
    groups_total: int
    output_dir: str
    duration: float
    budget_exhausted: bool = False
    resumed: bool = False


class PairedGroupCampaign:
    def __init__(self, sequence: SessionSequence, condition: DifferentialCondition,
                 budget: Budget, output_dir: str | Path, seed: int,
                 groups: int = 2, pairs_per_group: int = 2,
                 order_mode: str = "alternating", restore_enabled: bool = True,
                 fresh: bool = False) -> None:
        self.sequence = sequence
        self.condition = condition
        self.budget = budget
        self.output_dir = Path(output_dir)
        self.seed = seed
        self.groups = groups
        self.pairs_per_group = pairs_per_group
        self.order_mode = order_mode
        self.restore_enabled = restore_enabled
        self.fresh = fresh
        self.records: list[dict] = []
        self.next_group = 0
        self.next_pair = 0
        self.prior_spent = 0
        self.resumed = False

    # -- state locations -------------------------------------------------

    def _state_dir(self) -> Path:
        return self.output_dir / "shared_state"

    def _seed_dir(self) -> Path:
        return self.output_dir / "state_seed"

    def _snapshot_dir(self, group: int) -> Path:
        return self.output_dir / "snapshots" / f"group-{group}"

    # -- campaign ---------------------------------------------------------

    def run(self) -> PairedGroupSummary:
        start = time.monotonic()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._materialize_seed()
        if not self.fresh:
            self._load_checkpoint()
        spent = 0
        budget = self.budget.max_cases
        for group in range(self.next_group, self.groups):
            snapshot_dir = self._snapshot_dir(group)
            if not (snapshot_dir / preparation.MANIFEST_NAME).exists():
                preparation.freeze(self._seed_dir(), snapshot_dir)
            first_pair = self.next_pair if group == self.next_group else 0
            for pair in range(first_pair, self.pairs_per_group):
                if spent >= budget:
                    break
                record = self._run_pair(group, pair, snapshot_dir)
                self.records.append(record)
                spent += 1
                self.next_pair = pair + 1
                self._write_checkpoint()
            else:
                self.next_group = group + 1
                self.next_pair = 0
                self._write_checkpoint()
                continue
            break
        summary = PairedGroupSummary(
            self.seed,
            self.prior_spent + spent,
            self.next_group,
            self.groups,
            str(self.output_dir),
            time.monotonic() - start,
            self.next_group < self.groups,
            self.resumed,
        )
        self._write_summary(summary)
        return summary

    def _materialize_seed(self) -> None:
        seed_dir = self._seed_dir()
        if seed_dir.exists():
            return
        seed_dir.mkdir(parents=True, exist_ok=True)
        baseline = self._baseline_files()
        for name, data in baseline.items():
            target = seed_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    def _baseline_files(self) -> dict[str, bytes]:
        for version in (self.condition.old, self.condition.new):
            module_name = version.replay_name.rsplit(".", 1)[0]
            try:
                module = load_callable(module_name + ".baseline_files")
            except (ImportError, AttributeError):
                continue
            return dict(module())
        return {}

    def _pair_order(self, group: int, pair: int) -> tuple:
        index = group * self.pairs_per_group + pair
        if self.order_mode == "alternating" and index % 2 == 1:
            return (self.condition.new, self.condition.old)
        return (self.condition.old, self.condition.new)

    def _run_pair(self, group: int, pair: int, snapshot_dir: Path) -> dict:
        versions = self._pair_order(group, pair)
        order = tuple(version.identity for version in versions)
        record = {
            "group": group,
            "pair": pair,
            "name": f"group-{group}-pair-{pair}",
            "input_order": [self.condition.old.identity, self.condition.new.identity],
            "execution_order": list(order),
            "snapshot": str(snapshot_dir),
            "snapshot_manifest_sha256": preparation.load_manifest(snapshot_dir)["manifest_sha256"],
            "restore_enabled": self.restore_enabled,
            "preparation": {},
            "old": {},
            "new": {},
            "executed_sides": [],
            "failure_position": "",
            "disposition": "incomplete",
        }
        state_dir = self._state_dir()
        # Every pair starts from the group's frozen snapshot; with restore
        # disabled the seed is materialized once per pair but nothing is
        # restored between the two sides, so the first side's pollution
        # reaches the second side.
        try:
            preparation.restore(snapshot_dir, state_dir)
        except preparation.PreparationRestoreError as exc:
            record["disposition"] = "preparation_failed"
            record["failure_position"] = f"restore:pair-start:{exc}"
            return record
        previous = None
        for version in versions:
            if self.restore_enabled and previous is not None:
                try:
                    evidence = preparation.restore(snapshot_dir, state_dir)
                except preparation.PreparationRestoreError as exc:
                    record["disposition"] = "preparation_failed"
                    record["failure_position"] = (
                        f"restore:before-{version.identity}-after-{previous.identity}:{exc}"
                    )
                    return record
                record["preparation"][version.identity] = evidence
            elif self.restore_enabled:
                record["preparation"][version.identity] = preparation.verify(
                    snapshot_dir, state_dir
                )
            result = self._run_side(version, record["name"], len(record["executed_sides"]))
            record[version.identity] = result.to_dict()
            record["executed_sides"].append(version.identity)
            previous = version
        old_result = record.get(self.condition.old.identity) or None
        new_result = record.get(self.condition.new.identity) or None
        record["disposition"] = classify_pair(old_result, new_result, self.condition)
        return record

    def _run_side(self, version, namespace: str, ordinal: int):
        os.environ["GSB_SHARED_STATE_DIR"] = str(self._state_dir())
        try:
            return replay_sequence_isolated(
                self.sequence,
                version.replay_name,
                version.describe_name,
                self.budget.timeout,
                attempt_namespace=f"{namespace}-{version.identity}-{ordinal}",
            )
        finally:
            os.environ.pop("GSB_SHARED_STATE_DIR", None)

    # -- persistence ------------------------------------------------------

    def _write_checkpoint(self) -> None:
        payload = {
            "seed": self.seed,
            "pairs_executed": len(self.records),
            "groups": self.groups,
            "pairs_per_group": self.pairs_per_group,
            "order_mode": self.order_mode,
            "restore_enabled": self.restore_enabled,
            "next_group": self.next_group,
            "next_pair": self.next_pair,
            "condition": self.condition.to_dict(),
            "records": self.records,
        }
        _atomic_json(self.output_dir / CHECKPOINT_NAME, payload)

    def _load_checkpoint(self) -> None:
        path = self.output_dir / CHECKPOINT_NAME
        if not path.exists():
            return
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.records = list(payload.get("records", []))
        self.prior_spent = len(self.records)
        self.next_group = int(payload.get("next_group", 0))
        self.next_pair = int(payload.get("next_pair", 0))
        self.resumed = True

    def _write_summary(self, summary: PairedGroupSummary) -> None:
        totals = _count_dispositions(self.records)
        groups = []
        for group in range(self.groups):
            group_records = [item for item in self.records if item["group"] == group]
            snapshot_dir = self._snapshot_dir(group)
            manifest_sha = ""
            if (snapshot_dir / preparation.MANIFEST_NAME).exists():
                manifest_sha = preparation.load_manifest(snapshot_dir)["manifest_sha256"]
            groups.append({
                "group": group,
                "executed": bool(group_records) or group < self.next_group,
                "pairs": len(group_records),
                "snapshot_manifest_sha256": manifest_sha,
                "counts": _count_dispositions(group_records),
            })
        conclusion = order_conclusion([
            {"disposition": item["disposition"], "execution_order": item["execution_order"]}
            for item in self.records
        ])
        notes = []
        if totals.get("preparation_failed"):
            notes.append(
                f"{totals['preparation_failed']} pair(s) lost their preparation state; "
                "the failure position and executed sides are kept and no replacement "
                "sample was drawn"
            )
        if conclusion["order_conditioned"] and conclusion["verdict"] not in (
            "no_valid_rounds", "no_valid_hit"
        ):
            notes.append(
                "cross-group conclusion remains order-conditioned: "
                + conclusion["explanation"]
            )
        payload = {
            "seed": summary.seed,
            "pairs_executed": summary.pairs_executed,
            "groups_total": self.groups,
            "groups_completed": summary.groups_completed,
            "groups_remaining": self.groups - summary.groups_completed,
            "duration": summary.duration,
            "budget": {
                "max_pairs": self.budget.max_cases,
                "timeout": self.budget.timeout,
            },
            "budget_exhausted": summary.budget_exhausted,
            "resumed": summary.resumed,
            "restore_enabled": self.restore_enabled,
            "order_mode": self.order_mode,
            "condition": self.condition.to_dict(),
            "outcomes": totals,
            "groups_detail": groups,
            "order_conclusion": conclusion,
            "order_conditioned_notes": notes,
            "files": [CHECKPOINT_NAME, SUMMARY_NAME],
        }
        _atomic_json(self.output_dir / SUMMARY_NAME, payload)


def _count_dispositions(records: list[dict]) -> dict:
    counts = {"hit": 0, "rejected": 0, "timeout": 0,
              "preparation_failed": 0, "incomplete": 0}
    for record in records:
        disposition = record["disposition"]
        counts[disposition] = counts.get(disposition, 0) + 1
    counts["valid"] = counts["hit"] + counts["rejected"]
    return counts
