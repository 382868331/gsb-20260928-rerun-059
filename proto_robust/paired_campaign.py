"""Paired replay of version groups from frozen shared-preparation snapshots.

Each paired group freezes its shared on-disk preparation once (see
proto_robust/preparation.py).  Every pair then runs the two versions in an
explicit execution order -- pairs alternate old->new and new->old -- and
*every side* starts from the snapshot restored into the runtime shared
directory and verified by content hash.  Restoration is therefore backed
by per-file SHA-256 evidence, never by the directory merely existing.

If the preparation cannot be restored for one side, the pair is recorded
as preparation_failed with the failure location and the parts that
actually ran; the pair is never re-sampled to complete it.  The campaign
checkpoints after every pair, so an interrupted run resumes from the
first unexecuted pair of the first unfinished group.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import time

from proto_robust.compensation import (
    COMPENSATION_STRATEGIES,
    compare_schemes,
    plan_compensation,
)
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

ORDER_STRATEGIES = ("cross_group", "per_group")


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
                 order_strategy: str = "cross_group") -> None:
        if order_strategy not in ORDER_STRATEGIES:
            raise ValueError(f"unknown order strategy: {order_strategy}")
        self.groups = groups
        self.versions = {old.identity: old, new.identity: new}
        self.order = (old, new)
        self.budget = budget
        self.output_dir = Path(output_dir)
        self.seed = seed
        self.restore_between_sides = restore_between_sides
        self.fresh = fresh
        self.order_strategy = order_strategy
        self.plan = self._build_plan(order_strategy)
        self.records: list[dict] = []
        self.spent = 0
        self.resumed = False
        self.compensation_report: dict | None = None
        if not self.fresh:
            self._load_checkpoint()

    def _build_plan(self, strategy: str) -> list[dict]:
        """Fix the planned execution order of every pair before any run.

        cross_group: one alternation counter shared across groups, so odd
        group sizes keep the global old->new / new->old counts balanced.
        per_group: each group alternates independently from old->new, so an
        odd-sized group ends with one extra old->new pair.
        """
        plan = []
        global_index = 0
        for group in self.groups:
            for pair_index in range(group.pairs):
                counter = pair_index if strategy == "per_group" else global_index
                order = self.order if counter % 2 == 0 else (self.order[1], self.order[0])
                plan.append({
                    "group": group.group_id,
                    "pair": pair_index,
                    "planned_order": [version.identity for version in order],
                })
                global_index += 1
        return plan

    def planned_order_counts(self) -> dict:
        counts: dict[str, int] = {}
        for entry in self.plan:
            key = "->".join(entry["planned_order"])
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _plan_entry(self, group_id: str, pair_index: int) -> dict:
        for entry in self.plan:
            if entry["group"] == group_id and entry["pair"] == pair_index:
                return entry
        raise KeyError(f"pair not in plan: {group_id}#{pair_index}")

    def _planned_order(self, group_id: str, pair_index: int) -> list:
        return list(self._plan_entry(group_id, pair_index)["planned_order"])

    def apply_compensation(self, strategy: str) -> dict:
        """Append compensation positions for the current gaps.

        The executed sequence is never reordered and failed placeholders
        stay occupied; new positions are appended after the existing plan
        and each records which gap produced it.  Gaps that already have a
        compensation entry are skipped, so a repeated request never
        appends a second copy.  An infeasible scheme leaves the plan
        untouched and reports the conflicting constraints.
        """
        if strategy not in COMPENSATION_STRATEGIES:
            raise ValueError(f"unknown compensation strategy: {strategy}")
        report = plan_compensation(self.plan, self.records, strategy)
        self.compensation_report = report
        if report["status"] == "ok" and report["additions"]:
            next_index: dict[str, int] = {}
            for entry in self.plan:
                group = entry["group"]
                next_index[group] = max(next_index.get(group, 0), entry["pair"] + 1)
            for addition in report["additions"]:
                group = addition["group"]
                self.plan.append({
                    "group": group,
                    "pair": next_index[group],
                    "planned_order": list(addition["planned_order"]),
                    "compensates": dict(addition["compensates"]),
                    "compensation_strategy": addition["compensation_strategy"],
                    "reason": addition["reason"],
                })
                next_index[group] += 1
        if report["status"] == "ok":
            self._write_checkpoint()
        return report

    def scheme_comparison(self) -> dict:
        """Side-by-side metrics of every compensation scheme."""
        return compare_schemes(self.plan, self.records)

    def missing_pairs(self) -> list[dict]:
        """Planned pairs that never executed, with the reason they are absent.

        Preparation failures and timeouts still occupy their planned slot, so
        only pairs with no record at all appear here.
        """
        reason = "budget_exhausted" if self.spent >= self.budget.max_cases else "interrupted"
        missing = []
        for entry in self.plan:
            if not self._pair_done(entry["group"], entry["pair"]):
                missing.append({
                    "group": entry["group"],
                    "pair": entry["pair"],
                    "planned_order": list(entry["planned_order"]),
                    "reason": reason,
                })
        return missing

    def run(self) -> PairedSummary:
        start = time.monotonic()
        exhausted = False
        groups_by_id = {group.group_id: group for group in self.groups}
        for group in self.groups:
            snapshot_dir = self._snapshot_dir(group)
            if not (snapshot_dir / "manifest.json").exists():
                freeze_preparation(group.template_dir, snapshot_dir)
        for entry in self.plan:
            if self._pair_done(entry["group"], entry["pair"]):
                continue
            if self.spent >= self.budget.max_cases:
                exhausted = True
                break
            record = self._run_pair(groups_by_id[entry["group"]], entry["pair"])
            self.records.append(record)
            self.spent += 1
            self._write_checkpoint()
        total_pairs = len(self.plan)
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

    def _run_pair(self, group: PairedGroupSpec, pair_index: int) -> dict:
        entry = self._plan_entry(group.group_id, pair_index)
        planned = list(entry["planned_order"])
        order = tuple(self.versions[identity] for identity in planned)
        record = {
            "group": group.group_id,
            "pair": pair_index,
            "planned_order": planned,
            "executed": [],
            "sides": {},
            "disposition": None,
            "failure_location": "",
        }
        if entry.get("compensates"):
            record["compensates"] = dict(entry["compensates"])
            record["compensation_strategy"] = entry.get("compensation_strategy")
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
        for entry in self.plan:
            if not self._pair_done(entry["group"], entry["pair"]):
                next_position = {"group": entry["group"], "pair": entry["pair"]}
                break
        payload = {
            "compensation": self.compensation_report,
            "seed": self.seed,
            "spent": self.spent,
            "next": next_position,
            "order_strategy": self.order_strategy,
            "plan": self.plan,
            "groups_complete": sorted(done),
            "restore_between_sides": self.restore_between_sides,
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
        self.compensation_report = payload.get("compensation")
        saved_plan = payload.get("plan")
        if saved_plan:
            # Resume keeps the original plan: a restarted run must not
            # re-alternate from the old version or switch strategy.
            self.plan = list(saved_plan)
            self.order_strategy = str(payload.get("order_strategy", self.order_strategy))

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
            "order_strategy": self.order_strategy,
            "planned_orders": self.planned_order_counts(),
            "missing_pairs": self.missing_pairs(),
            "compensation": self.compensation_report,
            "scheme_comparison": self.scheme_comparison(),
            "budget": {
                "max_pairs": self.budget.max_cases,
                "timeout": self.budget.timeout,
                "wall_time": self.budget.wall_time,
            },
            "versions": {name: spec.to_dict() for name, spec in self.versions.items()},
            "totals": summary.totals,
            "groups": groups_payload,
            "orders": {key: self._totals(records) for key, records in orders.items()},
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
