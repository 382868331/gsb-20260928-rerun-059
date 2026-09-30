from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import time

from proto_robust.generator import SimpleMessageGenerator
from proto_robust.models import (
    Budget,
    BudgetExhausted,
    Finding,
    Outcome,
    ReproEvidence,
    TargetResult,
    _safe_filename,
)
from proto_robust.reducer import reduce_finding
from proto_robust.runner import IsolatedWorker, load_callable

FAILURE_OUTCOMES = {Outcome.CRASH, Outcome.TIMEOUT, Outcome.RUNNER_ERROR}

DEFAULT_CONFIRM_TRIALS = 4
DEFAULT_STABLE_THRESHOLD = 0.5


@dataclass
class CampaignSummary:
    seed: int
    target: str
    cases_executed: int
    executions: int
    findings: int
    output_dir: str
    duration: float
    status: str = "completed"
    next_index: int = 0


class FuzzingCampaign:
    def __init__(
        self,
        target: str,
        budget: Budget,
        output_dir: str | Path,
        seed: int,
        *,
        confirm_trials: int = DEFAULT_CONFIRM_TRIALS,
        stable_threshold: float = DEFAULT_STABLE_THRESHOLD,
    ) -> None:
        self.target_name = target
        self.target = load_callable(target)
        self.budget = budget
        self.output_dir = Path(output_dir)
        self.seed = seed
        self.confirm_trials = confirm_trials
        self.stable_threshold = stable_threshold
        self.findings: list[Finding] = []
        self._seen: set[str] = set()
        self.executions = 0
        self.next_index = 0
        self.status = "completed"

    def run(self, resume: bool = False) -> CampaignSummary:
        if resume:
            self._load_checkpoint()
        start = time.monotonic()
        generator = SimpleMessageGenerator(self.seed)
        worker = IsolatedWorker(self.target_name)

        def execute(data: bytes) -> TargetResult:
            if self.executions >= (self.budget.max_executions or float("inf")):
                self.status = "budget_exhausted"
                raise BudgetExhausted(
                    f"execution budget {self.budget.max_executions} spent"
                )
            result = worker.run(data, self.budget.timeout)
            self.executions += 1
            return result

        for finding in (f for f in self.findings if not f.reduction_done):
            try:
                self._reduce(finding, execute)
            except BudgetExhausted:
                self.status = "budget_exhausted"
                break

        executed = 0
        interrupted = self.status == "budget_exhausted"
        try:
            for index, case in enumerate(generator):
                if index < self.next_index:
                    continue
                if executed >= self.budget.max_cases:
                    break
                if self.budget.wall_time is not None and time.monotonic() - start >= self.budget.wall_time:
                    self.status = "budget_exhausted"
                    break
                try:
                    result = execute(case.data)
                except BudgetExhausted:
                    interrupted = True
                    break
                executed += 1
                self.next_index = index + 1
                if result.outcome not in FAILURE_OUTCOMES:
                    continue
                key = result.signature.key()
                if key in self._seen:
                    continue
                try:
                    evidence = self._reproduce(case.data, result, execute)
                except BudgetExhausted:
                    # The triggering sample already executed once; keep its
                    # evidence so the original sample is never lost on resume.
                    evidence = ReproEvidence(
                        1,
                        1,
                        [f"{result.outcome.value}:{result.exception_type}@{result.site}#{result.condition}"],
                    )
                    self._seen.add(key)
                    finding = Finding(
                        result.signature,
                        case.data,
                        case.data,
                        case.index,
                        evidence.trials,
                        repro=evidence,
                        generated_label=case.label,
                    )
                    finding.save(self.output_dir)
                    self.findings.append(finding)
                    self._write_checkpoint()
                    interrupted = True
                    break
                if evidence is None:
                    # Never delete on a single non-reproduction: the first
                    # crash is already evidence, so keep it as a flaky finding.
                    evidence = ReproEvidence(1, 1, [f"{result.outcome.value}:{result.exception_type}@{result.site}"])
                self._seen.add(key)
                finding = Finding(
                    result.signature,
                    case.data,
                    case.data,
                    case.index,
                    evidence.trials,
                    repro=evidence,
                    generated_label=case.label,
                )
                finding.save(self.output_dir)
                self.findings.append(finding)
                self._write_checkpoint()
                if evidence.rate < 1.0:
                    flaky = True
                else:
                    flaky = False
                try:
                    self._reduce(finding, execute, flaky=flaky)
                except BudgetExhausted:
                    interrupted = True
                    break
        finally:
            worker.close()
        if interrupted:
            self.status = "budget_exhausted"
        duration = time.monotonic() - start
        summary = CampaignSummary(
            self.seed,
            self.target_name,
            executed,
            self.executions,
            len(self.findings),
            str(self.output_dir),
            duration,
            self.status,
            self.next_index,
        )
        self._write_checkpoint(summary)
        return summary

    def _reproduce(self, data: bytes, first: TargetResult, execute) -> ReproEvidence | None:
        hits = 1
        outcomes = [f"{first.outcome.value}:{first.exception_type}@{first.site}"]
        for _ in range(self.confirm_trials - 1):
            result = execute(data)
            outcomes.append(f"{result.outcome.value}:{result.exception_type}@{result.site}")
            if result.signature.key() == first.signature.key():
                hits += 1
        return ReproEvidence(hits, self.confirm_trials, outcomes)

    def _reduce(self, finding: Finding, execute, flaky: bool | None = None) -> None:
        if flaky is None:
            flaky = finding.repro is not None and finding.repro.rate < 1.0
        reduce_finding(
            finding,
            execute,
            flaky=flaky,
            confirm_trials=self.confirm_trials,
            threshold=self.stable_threshold,
            on_progress=lambda updated: (updated.save(self.output_dir), self._write_checkpoint()),
        )
        finding.save(self.output_dir)
        self._write_checkpoint()

    def _load_checkpoint(self) -> None:
        campaign_path = self.output_dir / "campaign.json"
        if not campaign_path.exists():
            return
        payload = json.loads(campaign_path.read_text(encoding="utf-8"))
        self.next_index = payload.get("next_index", 0)
        self.executions = payload.get("executions", 0)
        for record in payload.get("finding_records", []):
            path = self.output_dir / record["file"]
            if not path.exists():
                continue
            finding = Finding.from_dict(
                json.loads(path.read_text(encoding="utf-8")), self.output_dir
            )
            self.findings.append(finding)
            self._seen.add(finding.signature.key())

    def _write_checkpoint(self, summary: CampaignSummary | None = None) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        records = []
        for finding in self.findings:
            file_name = f"{_safe_filename(finding.signature.key())}.json"
            records.append(
                {
                    "key": finding.signature.key(),
                    "file": file_name,
                    "reduction_done": finding.reduction_done,
                }
            )
        payload = {
            "seed": self.seed,
            "target": self.target_name,
            "cases_executed": summary.cases_executed if summary else self.next_index,
            "executions": self.executions,
            "findings": len(self.findings),
            "duration": summary.duration if summary else 0.0,
            "status": summary.status if summary else self.status,
            "next_index": summary.next_index if summary else self.next_index,
            "finding_records": records,
            "finding_files": [record["file"] for record in records],
        }
        (self.output_dir / "campaign.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
