"""Paired replay of version groups from frozen shared-preparation snapshots.

Each paired group freezes its shared on-disk preparation once (see
proto_robust/preparation.py).  Every pair then runs the two versions in an
explicit execution order -- pairs alternate old->new and new->old -- and
*every side* starts from the snapshot restored into the runtime shared
directory and verified by content hash.  Restoration is therefore backed
by per-file SHA-256 evidence, never by the directory merely existing.

If the preparation cannot be restored for one side, the pair is recorded
as preparation_failed with the failure location and the parts that
actually ran; the pair is never re-sampled to complete it.  The same
holds for target timeouts: both occupy their planned order slot.

Execution-order balancing is explicit.  With ``per-group`` balance each
group alternates old->new / new->old independently (an odd-sized group
therefore contributes one extra old->new pair).  With ``cross-group``
balance the alternation continues across group boundaries, so a group
following an odd-sized group starts with new->old.  The full plan is
computed once, shown before the first pair runs, persisted in the
checkpoint, and reused verbatim on resume -- an interrupted run never
restarts the alternation from old->new.  The summary reports planned
counts per order, actually executed counts per order, and the missing
pairs (with their planned orders) when the budget stops the campaign
early.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import time

from proto_robust.differential import VersionSpec
from proto_robust.models import Budget, Outcome
from proto_robust.preparation import (
    RestoreReport,
    freeze_preparation,
    restore_preparation,
)
from proto_robust.session_campaign import _atomic_json
from proto_robust.session_runner import replay_sequence_isolated
from proto_robust.sequences import SessionSequence

CHECKPOINT_NAME = "paired.checkpoint.json"
SUMMARY_NAME = "paired.json"
SHARED_DIR_ENV = "SHARED_FILE_STATE_DIR"


@dataclass(frozen=True)
class PairedGroupSpec:
    """One paired group: one sequence, one shared preparation, N pairs."""

    group_id: str
    sequence: SessionSequence
    template_dir: str
    pairs: int
    expected: dict  # version identity -> {"outcome": ..., optional fields}

    @classmethod
    def from_dict(cls, payload: dict, base_dir: str | Path = ".") -> "PairedGroupSpec":
        template = Path(payload["shared_state_template"])
        if not template.is_absolute() and not template.exists():
            candidate = Path(base_dir) / template
            if candidate.exists():
                template = candidate
        return cls(
            group_id=str(payload["id"]),
            sequence=SessionSequence.from_dict(payload["sequence"]),
            template_dir=str(template),
            pairs=int(payload.get("pairs", 2)),
            expected=dict(payload.get("expected", {})),
        )


@dataclass
class PairedSummary:
    seed: int
    pairs_executed: int
    output_dir: str
    duration: float
    budget_exhausted: bool = False
    resumed: bool = False
    totals: dict = field(default_factory=dict)


def _matches_expected(result_payload: dict, expected: dict) -> bool:
    if not expected:
        return True
    for field_name in ("outcome", "exception_type", "fault_function", "fault_line"):
        want = expected.get(field_name)
        if want is not None and result_payload.get(field_name) != want:
            return False
    return True


class PairedReplayCampaign:
    def __init__(self, groups: list[PairedGroupSpec], old: VersionSpec, new: VersionSpec,
                 budget: Budget, output_dir: str | Path, seed: int,
                 restore_between_sides: bool = True, fresh: bool = False,
                 order_balance: str = "per-group") -> None:
        if order_balance not in ("per-group", "cross-group"):
            raise ValueError(f"unknown order balance: {order_balance}")
        self.groups = groups
        self.versions = {old.identity: old, new.identity: new}
        self.order = (old, new)
        self.budget = budget
        self.output_dir = Path(output_dir)
        self.seed = seed
        self.restore_between_sides = restore_between_sides
        self.fresh = fresh
        self.order_balance = order_balance
        self.records: list[dict] = []
        self.spent = 0
        self.resumed = False
        self._plan: dict[tuple[str, int], tuple] = {}

    def ensure_plan(self) -> None:
        """Load any checkpoint and build the execution-order plan.

        Called implicitly by run(); callers may invoke it beforehand to
        display the planned counts per execution order.
        """
        if not self._plan:
            if not self.fresh:
                self._load_checkpoint()
            if not self._plan:
                self._plan = self._build_plan(self.order_balance)

    def run(self) -> PairedSummary:
        start = time.monotonic()
        if not self.resumed and not self.fresh:
            self._load_checkpoint()
        self.ensure_plan()
        exhausted = False
        for group_index, group in enumerate(self.groups):
            snapshot_dir = self._snapshot_dir(group)
            if not (snapshot_dir / "manifest.json").exists():
                freeze_preparation(group.template_dir, snapshot_dir)
            for pair_index in range(group.pairs):
                if self._pair_done(group.group_id, pair_index):
                    continue
                if self.spent >= self.budget.max_cases:
                    exhausted = True
                    break
                order = self._plan[(group.group_id, pair_index)]
                record = self._run_pair(group, pair_index, order)
                self.records.append(record)
                self.spent += 1
                self._write_checkpoint()
            if exhausted:
                break
        total_pairs = sum(group.pairs for group in self.groups)
        if len(self.records) >= total_pairs:
            exhausted = False
        elif self.spent >= self.budget.max_cases:
            exhausted = True
        summary = PairedSummary(
            seed=self.seed,
            pairs_executed=len(self.records),
            output_dir=str(self.output_dir),
            duration=time.monotonic() - start,
            budget_exhausted=exhausted,
            resumed=self.resumed,
            totals=self._totals(self.records),
        )
        self._write_summary(summary)
        return summary

    def _build_plan(self, balance: str) -> dict[tuple[str, int], tuple]:
        plan: dict[tuple[str, int], tuple] = {}
        counter = 0
        for group in self.groups:
            for pair_index in range(group.pairs):
                slot = counter if balance == "cross-group" else pair_index
                if slot % 2 == 1:
                    plan[(group.group_id, pair_index)] = (self.order[1], self.order[0])
                else:
                    plan[(group.group_id, pair_index)] = self.order
                counter += 1
        return plan

    def planned_order_counts(self) -> dict:
        counts: dict[str, int] = {}
        for order in self._plan.values():
            key = "->".join(version.identity for version in order)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def missing_pairs(self) -> list[dict]:
        missing = []
        for group in self.groups:
            for pair_index in range(group.pairs):
                if self._pair_done(group.group_id, pair_index):
                    continue
                order = self._plan[(group.group_id, pair_index)]
                missing.append({
                    "group": group.group_id,
                    "pair": pair_index,
                    "planned_order": [version.identity for version in order],
                    "reason": "budget_exhausted",
                })
        return missing

    def _pair_done(self, group_id: str, pair_index: int) -> bool:
        return any(
            record["group"] == group_id and record["pair"] == pair_index
            for record in self.records
        )

    def _snapshot_dir(self, group: PairedGroupSpec) -> Path:
        return self.output_dir / "groups" / group.group_id / "preparation.snapshot"

    def _runtime_dir(self) -> Path:
        return self.output_dir / "runtime_shared"

    def _restore_for_side(self, group: PairedGroupSpec) -> RestoreReport:
        return restore_preparation(self._snapshot_dir(group), self._runtime_dir())

    def _run_pair(self, group: PairedGroupSpec, pair_index: int, order: tuple) -> dict:
        record = {
            "group": group.group_id,
            "pair": pair_index,
            "planned_order": [version.identity for version in order],
            "executed": [],
            "sides": {},
            "disposition": None,
            "failure_location": "",
        }
        timed_out = False
        for position, version in enumerate(order):
            # The first side always starts from a verified restore; the
            # second side only does when the campaign restores between
            # sides (otherwise pollution by the first side is visible).
            if position == 0 or self.restore_between_sides:
                report = self._restore_for_side(group)
            else:
                report = None
            side = {"preparation": report.to_dict() if report else None}
            record["sides"][version.identity] = side
            if report is not None and not report.verified:
                # Preparation could not be restored for this side: keep the
                # failure location and whatever already ran; never re-sample.
                record["disposition"] = "preparation_failed"
                record["failure_location"] = report.failure_location
                return record
            os.environ[SHARED_DIR_ENV] = str(self._runtime_dir())
            result = replay_sequence_isolated(
                group.sequence,
                version.replay_name,
                version.describe_name,
                self.budget.timeout,
                attempt_namespace=f"paired-{group.group_id}-{pair_index}-{version.identity}",
            )
            side["result"] = result.to_dict()
            record["executed"].append(version.identity)
            if result.outcome == Outcome.TIMEOUT.value:
                timed_out = True
        if timed_out:
            record["disposition"] = "timeout"
            return record
        hits = all(
            _matches_expected(
                record["sides"][version.identity].get("result", {}),
                group.expected.get(version.identity, {}),
            )
            for version in self.order
        )
        record["disposition"] = "hit" if hits else "miss"
        return record

    @staticmethod
    def _totals(records: list[dict]) -> dict:
        totals = {"pairs": len(records), "preparation_failed": 0, "timeout": 0,
                  "valid": 0, "hits": 0, "misses": 0}
        for record in records:
            disposition = record["disposition"]
            if disposition == "preparation_failed":
                totals["preparation_failed"] += 1
            elif disposition == "timeout":
                totals["timeout"] += 1
            else:
                totals["valid"] += 1
                if disposition == "hit":
                    totals["hits"] += 1
                else:
                    totals["misses"] += 1
        return totals

    def _write_checkpoint(self) -> None:
        done = {record["group"] for record in self.records}
        next_position = None
        for group in self.groups:
            for pair_index in range(group.pairs):
                if not self._pair_done(group.group_id, pair_index):
                    next_position = {"group": group.group_id, "pair": pair_index}
                    break
            if next_position:
                break
        payload = {
            "seed": self.seed,
            "spent": self.spent,
            "next": next_position,
            "groups_complete": sorted(done),
            "restore_between_sides": self.restore_between_sides,
            "order_balance": self.order_balance,
            "order_plan": {
                f"{group_id}:{pair_index}": [version.identity for version in order]
                for (group_id, pair_index), order in self._plan.items()
            },
            "versions": {name: spec.to_dict() for name, spec in self.versions.items()},
            "records": self.records,
        }
        _atomic_json(self.output_dir / CHECKPOINT_NAME, payload)

    def _load_checkpoint(self) -> None:
        path = self.output_dir / CHECKPOINT_NAME
        if not path.exists():
            return
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.records = list(payload.get("records", []))
        self.spent = int(payload.get("spent", len(self.records)))
        self.resumed = True
        # Resume reuses the persisted plan verbatim: the alternation never
        # restarts from old->new just because the campaign was reopened.
        saved_plan = payload.get("order_plan")
        if saved_plan:
            self.order_balance = payload.get("order_balance", self.order_balance)
            self._plan = {}
            for key, identities in saved_plan.items():
                group_id, pair_index = key.rsplit(":", 1)
                self._plan[(group_id, int(pair_index))] = tuple(
                    self.versions[identity] for identity in identities
                )

    def _write_summary(self, summary: PairedSummary) -> None:
        groups_payload = []
        for group in self.groups:
            records = [r for r in self.records if r["group"] == group.group_id]
            groups_payload.append({
                "group": group.group_id,
                "planned_pairs": group.pairs,
                **self._totals(records),
            })
        orders: dict[str, list[dict]] = {}
        for record in self.records:
            key = "->".join(record["planned_order"])
            orders.setdefault(key, []).append(record)
        payload = {
            "seed": summary.seed,
            "duration": summary.duration,
            "resumed": summary.resumed,
            "budget_exhausted": summary.budget_exhausted,
            "restore_between_sides": self.restore_between_sides,
            "order_balance": self.order_balance,
            "order_plan": self.planned_order_counts(),
            "budget": {
                "max_pairs": self.budget.max_cases,
                "timeout": self.budget.timeout,
                "wall_time": self.budget.wall_time,
            },
            "versions": {name: spec.to_dict() for name, spec in self.versions.items()},
            "totals": summary.totals,
            "groups": groups_payload,
            "orders": {key: self._totals(records) for key, records in orders.items()},
            "missing_pairs": self.missing_pairs(),
            "records": self.records,
            "files": [CHECKPOINT_NAME, SUMMARY_NAME],
        }
        _atomic_json(self.output_dir / SUMMARY_NAME, payload)


def load_paired_goal(path: str | Path) -> tuple[list[PairedGroupSpec], dict]:
    """Load a paired-replay goal file with inline group sequences."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    base_dir = Path(path).parent
    groups = [PairedGroupSpec.from_dict(item, base_dir) for item in payload.get("groups", [])]
    return groups, payload
