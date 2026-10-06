"""Mid-experiment compensation scheduling for paired replay campaigns.

The paired plan is fixed before the experiment starts.  When individual
pairs fail (preparation failure, timeout) or a participant drops out, the
executed slots stay occupied -- a failed placeholder is evidence and is
never erased -- and the researcher picks one of three compensation
schemes:

- ``keep_gaps``: leave the gaps; nothing is appended.
- ``fill_failed``: append one replacement position per gap, reusing the
  failed slot's group and planned execution order.
- ``append_balanced``: fill every gap and then append extra positions so
  each affected group ends with balanced execution-order counts.

Compensation positions may only be appended after the existing sequence:
executed pairs are never reordered and unexecuted planned positions keep
their slots.  Three constraints are enforced simultaneously:

- odd-sized groups may legitimately lean toward one order, so balance is
  evaluated per group rather than by forcing a global alternation;
- all groups share a single execution resource, so appended positions form
  one global tail sequence (no parallel lanes);
- the same object (a group's shared preparation / participant) may not
  participate in two consecutive positions, including the boundary between
  the existing sequence and the appended tail.

When no ordering of the required additions satisfies every constraint,
the scheme is reported infeasible with the conflicting constraints named.
Every appended position records which gap produced it, and requesting
compensation twice never appends a second copy of an already-compensated
gap.
"""

from __future__ import annotations

COMPENSATION_STRATEGIES = ("keep_gaps", "fill_failed", "append_balanced")

# Dispositions that turn an executed pair into a gap.  A gap keeps its
# planned slot; it is never re-sampled in place and never erased.
GAP_DISPOSITIONS = ("preparation_failed", "timeout", "dropout")


def find_gaps(plan: list[dict], records: list[dict]) -> list[dict]:
    """Executed plan positions whose pair failed or dropped out.

    Compensation positions never become gaps themselves: a replacement
    that fails is recorded like any other pair, but it does not spawn a
    second generation of compensation, so repeated requests can never
    append duplicate chains.
    """
    by_key = {(record["group"], record["pair"]): record for record in records}
    gaps = []
    for entry in plan:
        if entry.get("compensates"):
            continue
        record = by_key.get((entry["group"], entry["pair"]))
        if record is not None and record.get("disposition") in GAP_DISPOSITIONS:
            gaps.append({
                "group": entry["group"],
                "pair": entry["pair"],
                "planned_order": list(entry["planned_order"]),
                "disposition": record["disposition"],
            })
    return gaps


def effective_entries(plan: list[dict], records: list[dict]) -> list[dict]:
    """The full execution sequence in plan order with per-slot status."""
    by_key = {(record["group"], record["pair"]): record for record in records}
    entries = []
    for entry in plan:
        record = by_key.get((entry["group"], entry["pair"]))
        if record is None:
            status = "pending"
        elif record.get("disposition") in GAP_DISPOSITIONS:
            status = "gap"
        else:
            status = "valid"
        entries.append({
            "group": entry["group"],
            "pair": entry["pair"],
            "planned_order": list(entry["planned_order"]),
            "status": status,
            "compensates": entry.get("compensates"),
        })
    return entries


def sequence_metrics(entries: list[dict]) -> dict:
    """Order balance, adjacent repeats and combination coverage.

    Gap slots still occupy their position (they influence adjacency) but
    contribute nothing to balance or coverage.
    """
    order_keys = sorted({"->".join(entry["planned_order"]) for entry in entries})
    active = [entry for entry in entries if entry["status"] != "gap"]
    per_group: dict[str, dict] = {}
    for entry in active:
        counts = per_group.setdefault(entry["group"], {key: 0 for key in order_keys})
        counts["->".join(entry["planned_order"])] += 1
    imbalance = {
        group: (max(counts.values()) - min(counts.values())) if counts else 0
        for group, counts in per_group.items()
    }
    adjacent_same_order = sum(
        1 for first, second in zip(entries, entries[1:])
        if first["planned_order"] == second["planned_order"]
    )
    adjacent_same_group = sum(
        1 for first, second in zip(entries, entries[1:])
        if first["group"] == second["group"]
    )
    groups = sorted({entry["group"] for entry in entries})
    covered = {
        (entry["group"], "->".join(entry["planned_order"])) for entry in active
    }
    missing = [
        {"group": group, "order": key}
        for group in groups for key in order_keys
        if (group, key) not in covered
    ]
    return {
        "positions": len(entries),
        "gaps": sum(1 for entry in entries if entry["status"] == "gap"),
        "order_balance_per_group": per_group,
        "order_imbalance_per_group": imbalance,
        "total_order_imbalance": sum(imbalance.values()),
        "adjacent_same_order": adjacent_same_order,
        "adjacent_same_group": adjacent_same_group,
        "combinations_covered": len(covered),
        "combinations_total": len(groups) * len(order_keys),
        "combinations_missing": missing,
    }


def _order_additions(required: list[dict], previous_group: str | None):
    """Order appended positions so no object participates twice in a row.

    Returns None when the single shared execution resource cannot separate
    the required positions of the dominant object.
    """
    remaining = list(required)
    ordered = []
    previous = previous_group
    while remaining:
        counts: dict[str, int] = {}
        for item in remaining:
            counts[item["group"]] = counts.get(item["group"], 0) + 1
        candidates = [group for group in counts if group != previous]
        if not candidates:
            return None
        chosen = max(candidates, key=lambda group: counts[group])
        item = next(entry for entry in remaining if entry["group"] == chosen)
        remaining.remove(item)
        ordered.append(item)
        previous = chosen
    return ordered


def _conflicts(required: list[dict], previous_group: str | None) -> list[dict]:
    counts: dict[str, int] = {}
    for item in required:
        counts[item["group"]] = counts.get(item["group"], 0) + 1
    dominant = max(counts, key=lambda group: counts[group])
    total = len(required)
    conflicts = [{
        "constraint": "no_consecutive_same_object",
        "detail": (
            f"object '{dominant}' needs {counts[dominant]} of {total} appended "
            f"positions; at most {total - counts[dominant] + 1} can be placed "
            "without the same object participating twice in a row"
        ),
    }, {
        "constraint": "single_shared_execution_resource",
        "detail": (
            "all groups share one execution resource, so appended positions "
            "form a single tail sequence and cannot run in parallel lanes"
        ),
    }, {
        "constraint": "executed_order_immutable",
        "detail": (
            "executed pairs cannot be reordered and failed placeholders "
            "cannot be erased, so no earlier slot can be freed to separate "
            "the appended positions"
        ),
    }]
    if previous_group == dominant:
        conflicts.append({
            "constraint": "sequence_boundary",
            "detail": (
                f"the existing sequence already ends with object "
                f"'{previous_group}', so the first appended position cannot "
                "absorb one of its slots"
            ),
        })
    return conflicts


def plan_compensation(plan: list[dict], records: list[dict],
                      strategy: str) -> dict:
    """Compute the appended positions for one compensation scheme.

    Pure function: the plan is not mutated.  Gaps that already have a
    compensation entry in the plan are skipped, so applying the same
    request twice never appends a duplicate.
    """
    if strategy not in COMPENSATION_STRATEGIES:
        raise ValueError(f"unknown compensation strategy: {strategy}")
    gaps = find_gaps(plan, records)
    compensated = {
        (entry["compensates"]["group"], entry["compensates"]["pair"])
        for entry in plan if entry.get("compensates")
    }
    pending = [gap for gap in gaps
               if (gap["group"], gap["pair"]) not in compensated]
    report = {
        "strategy": strategy,
        "status": "ok",
        "gaps": gaps,
        "already_compensated": [
            {"group": gap["group"], "pair": gap["pair"]}
            for gap in gaps if (gap["group"], gap["pair"]) in compensated
        ],
        "additions": [],
        "conflicts": [],
    }
    if strategy == "keep_gaps" or not pending:
        return report

    required = []
    for gap in pending:
        required.append({
            "group": gap["group"],
            "planned_order": list(gap["planned_order"]),
            "compensates": {"group": gap["group"], "pair": gap["pair"]},
            "reason": "fill_failed",
        })
    if strategy == "append_balanced":
        required.extend(_balancing_additions(plan, records, pending, required))

    previous_group = plan[-1]["group"] if plan else None
    ordered = _order_additions(required, previous_group)
    if ordered is None:
        report["status"] = "infeasible"
        report["conflicts"] = _conflicts(required, previous_group)
        report["additions"] = []
        return report
    for entry in ordered:
        entry["compensation_strategy"] = strategy
    report["additions"] = ordered
    return report


def _balancing_additions(plan: list[dict], records: list[dict],
                         pending_gaps: list[dict],
                         fill_additions: list[dict]) -> list[dict]:
    """Extra appended positions that rebalance each gap-affected group."""
    entries = effective_entries(plan, records)
    for addition in fill_additions:
        entries.append({
            "group": addition["group"],
            "pair": None,
            "planned_order": list(addition["planned_order"]),
            "status": "pending",
            "compensates": addition["compensates"],
        })
    order_keys = sorted({"->".join(entry["planned_order"]) for entry in entries})
    first_gap_by_group: dict[str, dict] = {}
    for gap in pending_gaps:
        first_gap_by_group.setdefault(gap["group"], gap)
    extras = []
    for group, gap in first_gap_by_group.items():
        counts = {key: 0 for key in order_keys}
        for entry in entries:
            if entry["group"] == group and entry["status"] != "gap":
                counts["->".join(entry["planned_order"])] += 1
        for extra in extras:
            if extra["group"] == group:
                counts["->".join(extra["planned_order"])] += 1
        while max(counts.values()) > min(counts.values()):
            minority = min(order_keys, key=lambda key: counts[key])
            extras.append({
                "group": group,
                "planned_order": minority.split("->"),
                "compensates": {"group": gap["group"], "pair": gap["pair"]},
                "reason": "append_balanced",
            })
            counts[minority] += 1
    return extras


def compare_schemes(plan: list[dict], records: list[dict]) -> dict:
    """Side-by-side metrics of every compensation scheme on this state."""
    comparison = {}
    for strategy in COMPENSATION_STRATEGIES:
        proposal = plan_compensation(plan, records, strategy)
        entries = effective_entries(plan, records)
        for addition in proposal["additions"]:
            entries.append({
                "group": addition["group"],
                "pair": None,
                "planned_order": list(addition["planned_order"]),
                "status": "pending",
                "compensates": addition["compensates"],
            })
        comparison[strategy] = {
            "status": proposal["status"],
            "conflicts": proposal["conflicts"],
            "additions": len(proposal["additions"]),
            "metrics": sequence_metrics(entries),
        }
    return comparison
