from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile
import time

from proto_robust.generator import SimpleMessageGenerator
from proto_robust.models import Budget, FailureSignature, Finding, Outcome
from proto_robust.reducer import reduce_case
from proto_robust.runner import IsolatedWorker, load_callable
from proto_robust.stability import confirm_failure

FAILURE_OUTCOMES = {Outcome.CRASH, Outcome.TIMEOUT, Outcome.RUNNER_ERROR}

# A finding must reproduce in at least one third of the trials; a single miss
# never deletes the sample, it only lowers the recorded stability rate.
CONFIRM_REPEATS = 5
CONFIRM_TIMEOUT_REPEATS = 2
CONFIRM_THRESHOLD = 1.0 / 3.0
CHECKPOINT_NAME = "campaign.checkpoint.json"


@dataclass
class CampaignSummary:
    seed: int
    target: str
    cases_executed: int
    findings: int
    output_dir: str
    duration: float
    budget_exhausted: bool = False
    resumed: bool = False


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


class FuzzingCampaign:
    def __init__(self, target: str, budget: Budget, output_dir: str | Path, seed: int) -> None:
        self.target_name = target
        self.target = load_callable(target)
        self.budget = budget
        self.output_dir = Path(output_dir)
        self.seed = seed
        self.findings: list[Finding] = []
        self._seen: set[str] = set()
        self.next_index = 0
        self.executed = 0
        self.prior_executed = 0
        self.started_at = 0.0
        self.resumed = False

    def run(self) -> CampaignSummary:
        start = time.monotonic()
        self._load_checkpoint()
        generator = SimpleMessageGenerator(self.seed)
        worker = IsolatedWorker(self.target_name)
        budget_exhausted = False
        try:
            for case in generator:
                if case.index < self.next_index:
                    continue
                if self.executed >= self.budget.max_cases:
                    budget_exhausted = True
                    break
                if self.budget.wall_time is not None and time.monotonic() - start >= self.budget.wall_time:
                    budget_exhausted = True
                    break
                result = worker.run(case.data, self.budget.timeout)
                self.executed += 1
                self.next_index = case.index + 1
                if result.outcome in FAILURE_OUTCOMES and result.signature.key() not in self._seen:
                    self._record_failure(worker, case.data, case.index, result)
                self._write_checkpoint()
        finally:
            worker.close()
        duration = time.monotonic() - start
        summary = CampaignSummary(
            self.seed,
            self.target_name,
            self.prior_executed + self.executed,
            len(self.findings),
            str(self.output_dir),
            duration,
            budget_exhausted,
            self.resumed,
        )
        self._write_summary(summary)
        return summary

    def _record_failure(self, worker: IsolatedWorker, data: bytes, index: int, first_result) -> None:
        signature = first_result.signature

        def run(sample: bytes):
            return worker.run(sample, self.budget.timeout)

        # Repeat before grouping so intermittent, timing-related failures keep
        # every trial and are not dismissed by one lucky run.
        repeats = CONFIRM_TIMEOUT_REPEATS if signature.outcome is Outcome.TIMEOUT else CONFIRM_REPEATS
        evidence = confirm_failure(lambda: run(data), signature, repeats)
        stable = evidence.hits / evidence.runs >= CONFIRM_THRESHOLD
        if not stable:
            # Preserve the raw sample plus evidence even when stability is weak.
            finding = Finding(
                signature,
                data,
                data,
                index,
                evidence.runs,
                confirmation=evidence.to_dict(),
                stable=False,
            )
            finding.save(self.output_dir)
            self.findings.append(finding)
            self._seen.add(signature.key())
            return

        report = reduce_case(data, signature, run)
        finding = Finding(
            signature,
            data,
            report.minimized,
            index,
            report.attempts,
            report.critical_byte_offsets,
            reduction_steps=report.steps,
            confirmation=evidence.to_dict(),
            stable=True,
        )
        finding.save(self.output_dir)
        self.findings.append(finding)
        self._seen.add(signature.key())

    def _checkpoint_path(self) -> Path:
        return self.output_dir / CHECKPOINT_NAME

    def _load_checkpoint(self) -> None:
        path = self._checkpoint_path()
        if not path.exists():
            return
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.next_index = int(payload.get("next_index", 0))
        # The resumed run receives a fresh budget; the checkpoint records the
        # cumulative count, but executed is reset so this run's budget is metered
        # against new cases only.
        self.started_at = float(payload.get("started_at", 0.0))
        self.resumed = True
        self.prior_executed = int(payload.get("cases_executed", 0))
        for item in payload.get("findings", []):
            self._seen.add(item["key"])
            finding_path = self.output_dir / item["file"]
            if finding_path.exists():
                record = json.loads(finding_path.read_text(encoding="utf-8"))
                signature_payload = record["signature"]
                signature = FailureSignature(
                    Outcome(signature_payload["outcome"]),
                    signature_payload.get("exception_type", ""),
                    signature_payload.get("message", ""),
                    signature_payload.get("fault_file", ""),
                    signature_payload.get("fault_function", ""),
                    signature_payload.get("fault_line", 0),
                    tuple(
                        (int(cond["offset"]), int(cond["value"]))
                        for cond in signature_payload.get("key_conditions", [])
                    ),
                )
                self.findings.append(Finding(
                    signature,
                    bytes.fromhex(record["original_hex"]),
                    bytes.fromhex(record["minimized_hex"]),
                    record["original_index"],
                    record.get("attempts", 0),
                    record.get("critical_byte_offsets", []),
                    reduction_steps=record.get("reduction_steps", []),
                    confirmation=record.get("confirmation", {}),
                    stable=record.get("stable", True),
                ))

    def _write_checkpoint(self) -> None:
        payload = {
            "seed": self.seed,
            "target": self.target_name,
            "budget": asdict(self.budget),
            "next_index": self.next_index,
            "cases_executed": self.prior_executed + self.executed,
            "seen_keys": sorted(self._seen),
            "findings": [
                {
                    "key": finding.signature.key(),
                    "original_index": finding.original_index,
                    "file": f"{finding.file_stem}.json",
                    "stable": finding.stable,
                }
                for finding in self.findings
            ],
        }
        _atomic_json(self._checkpoint_path(), payload)

    def _write_summary(self, summary: CampaignSummary) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "seed": summary.seed,
            "target": summary.target,
            "cases_executed": summary.cases_executed,
            "findings": summary.findings,
            "duration": summary.duration,
            "budget": asdict(self.budget),
            "budget_exhausted": summary.budget_exhausted,
            "resumed": summary.resumed,
            "next_index": self.next_index,
            "finding_files": [f"{finding.file_stem}.json" for finding in self.findings],
        }
        _atomic_json(self.output_dir / "campaign.json", payload)
