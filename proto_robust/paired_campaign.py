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
COMPENSATION_POLICIES = ("keep_gap", "replace_failed", "balanced_append")


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


def _slot_id(entry: dict) -> str:
    return str(entry.get("slot_id", f"{entry['group']}#{entry['pair']}"))


def _order_key(order: list[str] | tuple[str, ...]) -> str:
    return "->".join(order)


def _is_failed_record(record: dict) -> bool:
    return record.get("disposition") in ("preparation_failed", "timeout", "withdrawn")


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
                 order_strategy: str = "cross_group",
                 compensation_policy: str = "keep_gap") -> None:
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
        if compensation_policy not in COMPENSATION_POLICIES:
            raise ValueError(f"unknown compensation policy: {compensation_policy}")
        self.compensation_policy = compensation_policy
        self.plan = self._build_plan(order_strategy)
        self.records: list[dict] = []
        self.spent = 0
        self.resumed = False
        self.compensation_conflicts: list[dict] = []
        loaded = False
        if not self.fresh:
            loaded = self._load_checkpoint()
        if not loaded or self._all_originals_done():
            self.compensation_analysis_cache = self.apply_compensation_policy()

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
                    "slot_id": f"original:{group.group_id}:{pair_index}",
                    "group": group.group_id,
                    "pair": pair_index,
                    "planned_order": [version.identity for version in order],
                    "slot_kind": "original",
                    "source_gaps": [],
                    "compensation_status": "planned",
                })
                global_index += 1
        return plan

    def planned_order_counts(self) -> dict:
        counts: dict[str, int] = {}
        for entry in self.plan:
            key = "->".join(entry["planned_order"])
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _ordered_plan(self) -> list[dict]:
        return sorted(
            self.plan,
            key=lambda entry: (
                0 if entry.get("slot_kind") == "original" else 1,
                self._group_index(entry["group"]),
                int(entry["pair"]),
                int(entry.get("sequence", 0)),
            ),
        )

    def _group_index(self, group_id: str) -> int:
        for index, group in enumerate(self.groups):
            if group.group_id == group_id:
                return index
        raise KeyError(f"unknown group: {group_id}")

    def _record_by_slot(self, slot_id: str) -> dict | None:
        for record in self.records:
            if _slot_id(record) == slot_id:
                return record
        return None

    def _failed_gaps(self, include_covered: bool = False) -> list[dict]:
        covered_gaps = {
            source_gap
            for entry in self.plan
            for source_gap in entry.get("source_gaps", [])
        }
        gaps = []
        for entry in self.plan:
            if entry.get("slot_kind") != "original":
                continue
            record = self._record_by_slot(_slot_id(entry))
            slot_id = _slot_id(entry)
            if record is not None and _is_failed_record(record):
                covered_by = [
                    _slot_id(candidate)
                    for candidate in self.plan
                    if slot_id in candidate.get("source_gaps", [])
                ]
                if include_covered or slot_id not in covered_gaps:
                    gaps.append({
                    "slot_id": slot_id,
                    "group": entry["group"],
                    "pair": entry["pair"],
                    "planned_order": list(entry["planned_order"]),
                    "disposition": record.get("disposition"),
                    "failure_location": record.get("failure_location", ""),
                    "executed": list(record.get("executed", [])),
                    "covered_by": covered_by,
                })
        return gaps

    def _available_unexecuted_slots(self) -> list[dict]:
        return [
            entry for entry in self._ordered_plan()
            if self._record_by_slot(_slot_id(entry)) is None
        ]

    @staticmethod
    def _adjacent_repeats(entries: list[dict]) -> int:
        repeats = 0
        previous_order = None
        for entry in entries:
            order = tuple(entry["planned_order"])
            if previous_order is not None and order == previous_order:
                repeats += 1
            previous_order = order
        return repeats

    @staticmethod
    def _combination_coverage(entries: list[dict]) -> dict:
        coverage: dict[str, dict] = {}
        for entry in entries:
            order = tuple(entry["planned_order"])
            key = _order_key(order)
            group = coverage.setdefault(entry["group"], {"old->new": 0, "new->old": 0})
            group[key] = group.get(key, 0) + 1
        return coverage

    def _balance_metrics(self, entries: list[dict]) -> dict:
        per_group = self._combination_coverage(entries)
        imbalance = {
            group_id: abs(counts.get("old->new", 0) - counts.get("new->old", 0))
            for group_id, counts in per_group.items()
        }
        global_counts = {"old->new": 0, "new->old": 0}
        for counts in per_group.values():
            global_counts["old->new"] += counts.get("old->new", 0)
            global_counts["new->old"] += counts.get("new->old", 0)
        return {
            "per_group_order_counts": per_group,
            "global_order_counts": global_counts,
            "global_imbalance": abs(global_counts["old->new"] - global_counts["new->old"]),
            "maximum_group_imbalance": max(imbalance.values(), default=0),
            "adjacent_repeats": self._adjacent_repeats(entries),
        }

    @staticmethod
    def _effective_entries(entries: list[dict], gaps: list[dict]) -> list[dict]:
        gap_ids = {gap["slot_id"] for gap in gaps}
        return [entry for entry in entries if _slot_id(entry) not in gap_ids]

    def compensation_analysis(self) -> dict:
        """Compare the three legal compensation policies without mutating history."""
        all_gaps = self._failed_gaps(include_covered=True)
        gaps = self._failed_gaps()
        available = self._available_unexecuted_slots()
        available_by_group: dict[str, list[dict]] = {}
        for entry in available:
            available_by_group.setdefault(entry["group"], []).append(entry)

        baseline_entries = self._effective_entries(self._ordered_plan(), gaps)
        schemes: dict[str, dict] = {
            "keep_gap": {
                "additional_slots": [],
                "reused_unexecuted_slots": [],
                "retained_gaps": [gap["slot_id"] for gap in gaps],
                "conflicts": [],
                "metrics": self._balance_metrics(baseline_entries),
            }
        }

        reused: list[dict] = []
        unmatched = []
        for gap in gaps:
            candidates = available_by_group.get(gap["group"], [])
            same_order = next((
                entry for entry in candidates
                if entry.get("slot_kind") == "original"
                and entry["planned_order"] == gap["planned_order"]
                and entry["pair"] != gap["pair"]
                and _slot_id(entry) not in {item["slot_id"] for item in reused}
            ), None)
            if same_order is None:
                unmatched.append({
                    "gap": gap["slot_id"],
                    "constraint": "only an unexecuted same-group, same-order original position may replace it",
                })
            else:
                reused.append({
                    "slot_id": _slot_id(same_order),
                    "source_gap": gap["slot_id"],
                    "group": same_order["group"],
                    "pair": same_order["pair"],
                    "planned_order": list(same_order["planned_order"]),
                })
        replace_entries = self._effective_entries([
            dict(entry) for entry in self._ordered_plan()
        ], gaps)
        reused_ids = {item["slot_id"] for item in reused}
        for entry in replace_entries:
            if _slot_id(entry) in reused_ids:
                entry["source_gaps"] = [
                    item["source_gap"] for item in reused if _slot_id(entry) == item["slot_id"]
                ]
                entry["compensation_status"] = "reuses_unexecuted"
        schemes["replace_failed"] = {
            "additional_slots": [],
            "reused_unexecuted_slots": reused,
            "retained_gaps": [item["gap"] for item in unmatched],
            "conflicts": unmatched,
            "metrics": self._balance_metrics(replace_entries),
        }

        next_index = sum(1 for entry in self.plan if entry.get("slot_kind") == "compensation")
        covered_gaps = {
            source_gap
            for entry in self.plan
            for source_gap in entry.get("source_gaps", [])
        }
        appended = []
        effective_entries = self._effective_entries(self._ordered_plan(), gaps)
        effective_metrics = self._balance_metrics(effective_entries)
        allocation_conflicts = []
        for group in self.groups:
            group_gaps = [gap for gap in gaps if gap["group"] == group.group_id and gap["slot_id"] not in covered_gaps]
            counts = effective_metrics["per_group_order_counts"].get(
                group.group_id, {"old->new": 0, "new->old": 0}
            )
            count_on = counts.get("old->new", 0)
            count_no = counts.get("new->old", 0)
            target_count = max(count_on, count_no, 1)
            required_orders = (
                [["old", "new"]] * (target_count - count_on)
                + [["new", "old"]] * (target_count - count_no)
            )
            if required_orders and not group_gaps:
                allocation_conflicts.append({
                    "group": group.group_id,
                    "constraint": "balanced coverage requires an appended trial, but no failed/withdrawn gap can source it",
                })
                continue
            for required_index, order in enumerate(required_orders):
                gap = group_gaps[required_index % len(group_gaps)] if group_gaps else None
                if gap is None:
                    continue
                entry = {
                    "slot_id": f"compensation:{next_index}",
                    "group": gap["group"],
                    "pair": int(gap["pair"]),
                    "sequence": next_index,
                    "planned_order": list(order),
                    "slot_kind": "compensation",
                    "source_gaps": [gap["slot_id"]],
                    "compensation_status": "appended",
                }
                appended.append(entry)
                next_index += 1
        if self.order_strategy == "cross_group":
            ordered_appended: list[dict] = []
            remaining = list(appended)
            previous_order = (
                effective_entries[-1]["planned_order"] if effective_entries else None
            )
            while remaining:
                candidate = next((
                    entry for entry in remaining
                    if entry["planned_order"] != previous_order
                ), None)
                if candidate is None:
                    allocation_conflicts.append({
                        "constraint": "cross-group execution resource cannot avoid adjacent repeated treatment combinations",
                    })
                    break
                ordered_appended.append(candidate)
                remaining.remove(candidate)
                previous_order = candidate["planned_order"]
            appended = ordered_appended
            for sequence, entry in enumerate(appended):
                entry["sequence"] = sequence
                entry["slot_id"] = f"compensation:{sequence}"
        append_entries = effective_entries + appended
        conflicts: list[dict] = []
        conflicts.extend(allocation_conflicts)
        append_metrics = self._balance_metrics(append_entries)
        for group in self.groups:
            counts = append_metrics["per_group_order_counts"].get(group.group_id, {})
            if self.order_strategy == "per_group" and counts and (
                counts.get("old->new", 0) != counts.get("new->old", 0)
            ):
                conflicts.append({
                    "group": group.group_id,
                    "constraint": "balanced appends do not equalize the two treatment orders in this group",
                })
        if self.order_strategy == "cross_group" and append_metrics["global_imbalance"]:
            conflicts.append({
                "constraint": "balanced appends do not equalize the two treatment orders across the shared cross-group resource",
            })
        if append_metrics["adjacent_repeats"]:
            conflicts.append({
                "constraint": "the same treatment/order combination appears in adjacent executable slots",
                "adjacent_repeats": append_metrics["adjacent_repeats"],
            })
        schemes["balanced_append"] = {
            "additional_slots": [] if conflicts else appended,
            "reused_unexecuted_slots": [],
            "retained_gaps": [gap["slot_id"] for gap in gaps] if conflicts else [],
            "conflicts": conflicts,
            "metrics": append_metrics,
        }
        return {
            "policy": self.compensation_policy,
            "gaps": gaps,
            "all_gaps": all_gaps,
            "available_unexecuted_slots": [
                {
                    "slot_id": _slot_id(entry),
                    "group": entry["group"],
                    "pair": entry["pair"],
                    "planned_order": list(entry["planned_order"]),
                    "slot_kind": entry.get("slot_kind", "original"),
                }
                for entry in available
            ],
            "schemes": schemes,
        }

    def apply_compensation_policy(self) -> dict:
        analysis = self.compensation_analysis()
        scheme = analysis["schemes"][self.compensation_policy]
        existing_compensation = {_slot_id(entry) for entry in self.plan if entry.get("slot_kind") == "compensation"}
        for entry in scheme["additional_slots"]:
            if _slot_id(entry) not in existing_compensation:
                self.plan.append(entry)
        reused = {item["slot_id"]: item["source_gap"] for item in scheme["reused_unexecuted_slots"]}
        for entry in self.plan:
            slot_id = _slot_id(entry)
            if slot_id in reused:
                entry["source_gaps"] = [reused[slot_id]]
                entry["compensation_status"] = "reuses_unexecuted"
        if scheme["conflicts"]:
            self.compensation_conflicts = list(scheme["conflicts"])
        else:
            self.compensation_conflicts = []
        self.compensation_analysis_cache = analysis
        return analysis

    def _planned_order(self, group_id: str, pair_index: int) -> list:
        for entry in self.plan:
            if entry["group"] == group_id and entry["pair"] == pair_index:
                return list(entry["planned_order"])
        raise KeyError(f"pair not in plan: {group_id}#{pair_index}")

    def missing_pairs(self) -> list[dict]:
        """Planned pairs that never executed, with the reason they are absent.

        Preparation failures and timeouts still occupy their planned slot, so
        only pairs with no record at all appear here.
        """
        reason = "budget_exhausted" if self.spent >= self.budget.max_cases else "interrupted"
        missing = []
        for entry in self.plan:
            if not self._slot_done(_slot_id(entry)):
                missing.append({
                    "slot_id": _slot_id(entry),
                    "group": entry["group"],
                    "pair": entry["pair"],
                    "planned_order": list(entry["planned_order"]),
                    "slot_kind": entry.get("slot_kind", "original"),
                    "source_gaps": list(entry.get("source_gaps", [])),
                    "reason": reason,
                })
        return missing

    def run(self) -> PairedSummary:
        start = time.monotonic()
        exhausted = False
        groups_by_id = {group.group_id: group for group in self.groups}
        self._write_checkpoint()
        original_entries = [
            entry for entry in self._ordered_plan() if entry.get("slot_kind") == "original"
        ]
        for entry in original_entries:
            if self._slot_done(_slot_id(entry)):
                continue
            if self.spent >= self.budget.max_cases:
                exhausted = True
                break
            group = groups_by_id[entry["group"]]
            snapshot_dir = self._snapshot_dir(group)
            if not (snapshot_dir / "manifest.json").exists():
                freeze_preparation(group.template_dir, snapshot_dir)
            record = self._run_pair(group, entry)
            self.records.append(record)
            self.spent += 1
        self._write_checkpoint()
        if not exhausted:
            self.apply_compensation_policy()
            self._write_checkpoint()
        for entry in self._ordered_plan():
            if entry.get("slot_kind") == "original" or self._slot_done(_slot_id(entry)):
                continue
            if self.spent >= self.budget.max_cases:
                exhausted = True
                break
            group = groups_by_id[entry["group"]]
            snapshot_dir = self._snapshot_dir(group)
            if not (snapshot_dir / "manifest.json").exists():
                freeze_preparation(group.template_dir, snapshot_dir)
            record = self._run_pair(group, entry)
            self.records.append(record)
            self.spent += 1
            self._write_checkpoint()
        executable_slots = len(self._ordered_plan())
        if len(self.records) >= executable_slots:
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

    def _slot_done(self, slot_id: str) -> bool:
        return self._record_by_slot(slot_id) is not None

    def _all_originals_done(self) -> bool:
        return all(
            self._slot_done(_slot_id(entry))
            for entry in self.plan
            if entry.get("slot_kind") == "original"
        )

    def _pair_done(self, group_id: str, pair_index: int) -> bool:
        return self._slot_done(f"original:{group_id}:{pair_index}")

    def _snapshot_dir(self, group: PairedGroupSpec) -> Path:
        return self.output_dir / "groups" / group.group_id / "preparation.snapshot"

    def _runtime_dir(self) -> Path:
        return self.output_dir / "runtime_shared"

    def _restore_for_side(self, group: PairedGroupSpec) -> RestoreReport:
        return restore_preparation(self._snapshot_dir(group), self._runtime_dir())

    def _run_pair(self, group: PairedGroupSpec, plan_entry: dict) -> dict:
        pair_index = int(plan_entry["pair"])
        planned = list(plan_entry["planned_order"])
        order = tuple(self.versions[identity] for identity in planned)
        record = {
            "slot_id": _slot_id(plan_entry),
            "group": group.group_id,
            "pair": pair_index,
            "planned_order": planned,
            "slot_kind": plan_entry.get("slot_kind", "original"),
            "source_gaps": list(plan_entry.get("source_gaps", [])),
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
                attempt_namespace=f"paired-{_slot_id(plan_entry)}-{version.identity}",
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
                  "withdrawn": 0, "valid": 0, "hits": 0, "misses": 0}
        for record in records:
            disposition = record["disposition"]
            if disposition == "preparation_failed":
                totals["preparation_failed"] += 1
            elif disposition == "timeout":
                totals["timeout"] += 1
            elif disposition == "withdrawn":
                totals["withdrawn"] += 1
            else:
                totals["valid"] += 1
                if disposition == "hit":
                    totals["hits"] += 1
                else:
                    totals["misses"] += 1
        return totals

    def _write_checkpoint(self) -> None:
        done = {record["group"] for record in self.records}
        next_entry = next(
            (entry for entry in self._ordered_plan() if not self._slot_done(_slot_id(entry))),
            None,
        )
        next_position = None if next_entry is None else {
            "group": next_entry["group"],
            "pair": int(next_entry["pair"]),
        }
        payload = {
            "seed": self.seed,
            "spent": self.spent,
            "next": next_position,
            "next_slot_id": _slot_id(next_entry) if next_entry else None,
            "order_strategy": self.order_strategy,
            "compensation_policy": self.compensation_policy,
            "plan": self.plan,
            "groups_complete": sorted(done),
            "restore_between_sides": self.restore_between_sides,
            "versions": {name: spec.to_dict() for name, spec in self.versions.items()},
            "records": self.records,
        }
        _atomic_json(self.output_dir / CHECKPOINT_NAME, payload)

    def _load_checkpoint(self) -> bool:
        path = self.output_dir / CHECKPOINT_NAME
        if not path.exists():
            return False
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.records = []
        for record in payload.get("records", []):
            normalized = dict(record)
            if "slot_id" not in normalized:
                normalized["slot_id"] = f"original:{normalized['group']}:{normalized['pair']}"
            self.records.append(normalized)
        self.spent = int(payload.get("spent", len(self.records)))
        self.resumed = True
        saved_plan = payload.get("plan")
        if saved_plan:
            # Resume keeps the original plan: a restarted run must not
            # re-alternate from the old version or switch strategy.
            self.plan = []
            for entry in saved_plan:
                normalized = dict(entry)
                normalized.setdefault("slot_id", f"original:{normalized['group']}:{normalized['pair']}")
                normalized.setdefault("slot_kind", "original")
                normalized.setdefault("source_gaps", [])
                normalized.setdefault("compensation_status", "planned")
                self.plan.append(normalized)
            self.order_strategy = str(payload.get("order_strategy", self.order_strategy))
        saved_compensation = payload.get("compensation_policy")
        if saved_compensation:
            if saved_compensation not in COMPENSATION_POLICIES:
                raise ValueError(f"unknown saved compensation policy: {saved_compensation}")
            self.compensation_policy = str(saved_compensation)
        return True

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
            "compensation_policy": self.compensation_policy,
            "compensation": self.compensation_analysis(),
            "planned_orders": self.planned_order_counts(),
            "missing_pairs": self.missing_pairs(),
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
