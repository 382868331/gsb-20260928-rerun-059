from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import time

from proto_robust.generator import SimpleMessageGenerator
from proto_robust.models import Budget, Finding, Outcome
from proto_robust.reducer import reduce_case
from proto_robust.runner import IsolatedWorker, load_callable

FAILURE_OUTCOMES = {Outcome.CRASH, Outcome.TIMEOUT, Outcome.RUNNER_ERROR}


@dataclass
class CampaignSummary:
    seed: int
    target: str
    cases_executed: int
    findings: int
    output_dir: str
    duration: float


class FuzzingCampaign:
    def __init__(self, target: str, budget: Budget, output_dir: str | Path, seed: int) -> None:
        self.target_name = target
        self.target = load_callable(target)
        self.budget = budget
        self.output_dir = Path(output_dir)
        self.seed = seed
        self.findings: list[Finding] = []
        self._seen: set[str] = set()

    def run(self) -> CampaignSummary:
        start = time.monotonic()
        generator = SimpleMessageGenerator(self.seed)
        executed = 0
        worker = IsolatedWorker(self.target_name)
        try:
            for case in generator:
                if executed >= self.budget.max_cases:
                    break
                if self.budget.wall_time is not None and time.monotonic() - start >= self.budget.wall_time:
                    break
                result = worker.run(case.data, self.budget.timeout)
                executed += 1
                if result.outcome in FAILURE_OUTCOMES and result.signature.key() not in self._seen:
                    self._seen.add(result.signature.key())
                    minimized, attempts, critical_offsets = reduce_case(
                        case.data,
                        result.signature,
                        lambda data: worker.run(data, self.budget.timeout),
                    )
                    finding = Finding(
                        result.signature,
                        case.data,
                        minimized,
                        case.index,
                        attempts,
                        critical_offsets,
                    )
                    finding.save(self.output_dir)
                    self.findings.append(finding)
        finally:
            worker.close()
        duration = time.monotonic() - start
        summary = CampaignSummary(
            self.seed,
            self.target_name,
            executed,
            len(self.findings),
            str(self.output_dir),
            duration,
        )
        self._write_summary(summary)
        return summary

    def _write_summary(self, summary: CampaignSummary) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "seed": summary.seed,
            "target": summary.target,
            "cases_executed": summary.cases_executed,
            "findings": summary.findings,
            "duration": summary.duration,
            "finding_files": [
                f"{finding.signature.key().replace('/', '_')}.json" for finding in self.findings
            ],
        }
        (self.output_dir / "campaign.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
