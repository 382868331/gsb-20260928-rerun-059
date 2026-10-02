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
                 fresh: bool = False) -> None:
        self.sequence = sequence
        self.condition = condition
        self.budget = budget
        self.output_dir = Path(output_dir)
        self.seed = seed
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

    def run(self) -> DifferentialSummary:
        start = time.monotonic()
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

    def _run_pair(self, sequence: SessionSequence, namespace: str, mode: str):
        order = (self.condition.old, self.condition.new)
        results = []
        for ordinal, version in enumerate(order):
            results.append(replay_sequence_isolated(
                sequence,
                version.replay_name,
                version.describe_name,
                self.budget.timeout,
                attempt_namespace=f"{namespace}-{version.identity}-{ordinal}",
            ))
        return results[0], results[1]

    def _write_checkpoint(self) -> None:
        payload = {
            "seed": self.seed,
            "pair_attempts": self.prior_spent + self.spent,
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
        if payload.get("reduction"):
            self.report = DifferentialReductionReport.from_checkpoint(payload["reduction"])
        if payload.get("final"):
            candidate = self.report.minimized if self.report else self.sequence
            self.final = CandidateEvaluation.from_dict(payload["final"], candidate)

    def _write_summary(self, summary: DifferentialSummary) -> None:
        reduction_steps = self.report.steps if self.report else []
        totals = {"hit": 0, "rejected": 0, "timeout": 0, "preparation_failed": 0}
        for step in reduction_steps:
            for name, value in step.get("counts", {}).items():
                totals[name] = totals.get(name, 0) + value
        if self.final:
            for name, value in self.final.counts().items():
                totals[name] = totals.get(name, 0) + value
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
            "input_order": [self.condition.old.identity, self.condition.new.identity],
            "files": [
                "original.sequence.json",
                "minimized.sequence.json",
                "final.sequence.json",
                CHECKPOINT_NAME,
                "differential.json",
            ],
        }
        _atomic_json(self.output_dir / "differential.json", payload)


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
