"""Resumable old/new differential comparison campaign."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import time

from proto_robust.differential import (
    CandidateEvaluation,
    AttemptPolicy,
    DifferentialCondition,
    DifferentialReductionReport,
    DifferenceKind,
    VersionSpec,
    evaluate_candidate,
    reduce_differential_sequence,
)
from proto_robust.models import Budget, FailureSignature
from proto_robust.session_campaign import _atomic_json, signature_from_result
from proto_robust.session_runner import replay_sequence_isolated
from proto_robust.sequences import ReplayResult, SessionSequence

CHECKPOINT_NAME = "differential.checkpoint.json"


@dataclass
class DifferentialSummary:
    seed: int
    pair_attempts: int
    accepted_candidates: int
    output_dir: str
    duration: float
    budget_exhausted: bool = False
    resumed: bool = False


class DifferentialCampaign:
    def __init__(self, sequence: SessionSequence, condition: DifferentialCondition,
                 budget: Budget, output_dir: str | Path, seed: int,
                 candidate_policy: AttemptPolicy | None = None,
                 final_policy: AttemptPolicy | None = None,
                 order_plan: tuple[str, ...] = ("fixed",),
                 fresh: bool = False) -> None:
        self.sequence = sequence
        self.condition = condition
        self.budget = budget
        self.output_dir = Path(output_dir)
        self.seed = seed
        self.order_plan = tuple(order_plan) or ("fixed",)
        self.candidate_policy = candidate_policy or AttemptPolicy(
            valid_attempts=2,
            stop_after_rejections=1,
            required_hit_ratio=1.0,
        )
        self.final_policy = final_policy or AttemptPolicy(valid_attempts=3)
        self.spent = 0
        self.prior_spent = 0
        self.resumed = False
        self.report: DifferentialReductionReport | None = None
        self.final = None
        self.fresh = fresh
        self._deadline: float | None = None

    def run(self) -> DifferentialSummary:
        start = time.monotonic()
        if self.budget.wall_time:
            self._deadline = start + self.budget.wall_time
        if not self.fresh:
            self._load_checkpoint()
        accepted = 0
        if not self.report or not self.report.complete:
            remaining = max(0, self.budget.max_cases - self.spent)
            self.report, consumed, exhausted = reduce_differential_sequence(
                self.sequence,
                self.condition,
                self._run_pair,
                policy=self.candidate_policy,
                order_plan=self.order_plan,
                report=self.report,
                max_pair_attempts=remaining,
                pair_attempts_used=0,
            )
            self.spent += consumed
            self.sequence.save(self.output_dir / "original.sequence.json")
            self.report.minimized.save(self.output_dir / "minimized.sequence.json")
            self._write_checkpoint()
        else:
            exhausted = self.spent >= self.budget.max_cases

        if self.report and self.report.complete and self.report.accepted and self.final is None:
            if self.spent < self.budget.max_cases:
                remaining = max(0, self.budget.max_cases - self.spent)
                self.final = evaluate_candidate(
                    self.report.minimized,
                    self.condition,
                    self._run_pair,
                    self.final_policy,
                    "final",
                    "group",
                    order_plan=self.order_plan,
                    attempt_budget=remaining,
                    attempts_before=0,
                )
                self.spent += min(remaining, len(self.final.attempts))
                self.report.minimized.save(self.output_dir / "final.sequence.json")
                self._write_checkpoint()
                accepted = 1 if self.final.accepted else 0
        elif self.final is not None:
            accepted = 1 if self.final.accepted else 0

        summary = DifferentialSummary(
            self.seed,
            self.prior_spent + self.spent,
            accepted,
            str(self.output_dir),
            time.monotonic() - start,
            self.spent >= self.budget.max_cases or (not (self.final and self.final.complete)),
            self.resumed,
        )
        self._write_summary(summary)
        return summary

    def _run_pair(self, sequence: SessionSequence, namespace: str, mode: str,
                  order: tuple[str, str]) -> dict:
        versions = {
            self.condition.old.identity: self.condition.old,
            self.condition.new.identity: self.condition.new,
        }
        results: dict[str, ReplayResult | None] = {}
        for ordinal, identity in enumerate(order):
            version = versions[identity]
            if self._deadline is not None and time.monotonic() >= self._deadline:
                # The wall clock cut the pair in half: the remaining side
                # never ran, so the round stays incomplete instead of
                # becoming a fabricated miss for that version.
                results[identity] = None
                continue
            try:
                results[identity] = replay_sequence_isolated(
                    sequence,
                    version.replay_name,
                    version.describe_name,
                    self.budget.timeout,
                    attempt_namespace=f"{namespace}-{version.identity}-{ordinal}",
                )
            except Exception:
                results[identity] = None
        return results

    def _write_checkpoint(self) -> None:
        payload = {
            "seed": self.seed,
            "pair_attempts": self.prior_spent + self.spent,
            "order_plan": list(self.order_plan),
            "condition": self.condition.to_dict(),
            "candidate_policy": self.candidate_policy.to_dict(),
            "final_policy": self.final_policy.to_dict(),
            "reduction": self.report.to_checkpoint() if self.report else None,
            "final": self.final.to_dict() if self.final else None,
        }
        _atomic_json(self.output_dir / CHECKPOINT_NAME, payload)

    def _load_checkpoint(self) -> None:
        path = self.output_dir / CHECKPOINT_NAME
        if not path.exists():
            return
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.prior_spent = int(payload.get("pair_attempts", 0))
        self.spent = 0
        self.resumed = True
        if payload.get("order_plan"):
            self.order_plan = tuple(payload["order_plan"])
        if payload.get("reduction"):
            self.report = DifferentialReductionReport.from_checkpoint(payload["reduction"])
        if payload.get("final"):
            candidate = self.report.minimized if self.report else self.sequence
            self.final = CandidateEvaluation.from_dict(payload["final"], candidate)

    def _write_summary(self, summary: DifferentialSummary) -> None:
        reduction_steps = self.report.steps if self.report else []
        totals = {"hit": 0, "rejected": 0, "timeout": 0, "preparation_failed": 0, "incomplete": 0}
        for step in reduction_steps:
            for name, value in step.get("counts", {}).items():
                totals[name] = totals.get(name, 0) + value
        if self.final:
            for name, value in self.final.counts().items():
                totals[name] = totals.get(name, 0) + value
        order_conclusion = None
        if self.final is not None:
            order_conclusion = self.final.order_conclusion()
        elif self.report is not None:
            accepted_steps = [
                step for step in reduction_steps
                if step.get("accepted") and step.get("order_conclusion")
            ]
            if accepted_steps:
                order_conclusion = accepted_steps[-1]["order_conclusion"]
        order_limited: list[str] = []
        if order_conclusion and order_conclusion.get("order_limited"):
            order_limited.append(order_conclusion["explanation"])
            prep_note = self._preparation_note(order_conclusion)
            if prep_note:
                order_limited.append(prep_note)
        payload = {
            "seed": summary.seed,
            "pair_attempts": summary.pair_attempts,
            "accepted_candidates": summary.accepted_candidates,
            "duration": summary.duration,
            "budget": {
                "max_pair_attempts": self.budget.max_cases,
                "timeout": self.budget.timeout,
                "wall_time": self.budget.wall_time,
            },
            "budget_exhausted": summary.budget_exhausted,
            "resumed": summary.resumed,
            "condition": self.condition.to_dict(),
            "outcomes": totals,
            "order_plan": list(self.order_plan),
            "input_order": [self.condition.old.identity, self.condition.new.identity],
            "order_conclusion": order_conclusion,
            "order_limited_conclusions": order_limited,
            "files": [
                "original.sequence.json",
                "minimized.sequence.json",
                "final.sequence.json",
                CHECKPOINT_NAME,
                "differential.json",
            ],
        }
        _atomic_json(self.output_dir / "differential.json", payload)

    @staticmethod
    def _preparation_note(order_conclusion: dict) -> str:
        notes = []
        for direction, stats in order_conclusion.get("per_order", {}).items():
            if stats.get("preparation_failed"):
                notes.append(
                    f"rounds ordered {direction} include "
                    f"{stats['preparation_failed']} preparation failure(s) that were "
                    "not counted as misses for either version"
                )
            if stats.get("incomplete"):
                notes.append(
                    f"rounds ordered {direction} include "
                    f"{stats['incomplete']} incomplete round(s) where one side never ran"
                )
        return "; ".join(notes)


def condition_from_old_result(result: ReplayResult, kind: str,
                              old_version: VersionSpec, new_version: VersionSpec,
                              new_signature: FailureSignature | None = None) -> DifferentialCondition:
    return DifferentialCondition(
        DifferenceKind(kind),
        old_version,
        new_version,
        signature_from_result(result),
        new_signature,
    )
