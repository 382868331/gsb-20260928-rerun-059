"""Budgeted multi-session robustness campaign with resumable reduction.

Each campaign sequence, confirmation trial and reduction candidate is a full
replay in a freshly spawned worker process, so failures never leak session
state into subsequent inputs.  Findings keep the original sequence, the best
minimized candidate and the full interaction order of every attempt.  When the
budget pauses work (during confirmation or reduction), checkpoints let a later
run continue without losing or replaying-for-free the earlier evidence.
"""

from __future__ import annotations
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile
import time

from proto_robust.generator import SessionSequenceGenerator
from proto_robust.models import Budget, FailureSignature, Outcome
from proto_robust.session_reducer import (
    SequenceReductionReport,
    reduce_sequence,
    same_failure,
)
from proto_robust.session_runner import replay_sequence_isolated
from proto_robust.sequences import ReplayResult, SessionSequence

FAILURE_OUTCOMES = {Outcome.CRASH.value, Outcome.TIMEOUT.value, Outcome.RUNNER_ERROR.value}

CONFIRM_REPEATS = 5
CONFIRM_TIMEOUT_REPEATS = 2
CONFIRM_THRESHOLD = 1.0 / 2.0
REDUCE_REPEATS = 3
CHECKPOINT_NAME = "session.campaign.checkpoint.json"


@dataclass
class SessionCampaignSummary:
    seed: int
    target: str
    replays: int
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


def signature_from_result(result: ReplayResult) -> FailureSignature:
    outcome = Outcome(result.outcome)
    return FailureSignature(
        outcome=outcome,
        exception_type=result.exception_type,
        message=result.message,
        fault_file=result.fault_file,
        fault_function=result.fault_function,
        fault_line=result.fault_line,
        state_path=result.state_path,
        step_key_conditions=tuple(result.key_conditions),
    )


class SessionCampaign:
    def __init__(self, replay_name: str, describe_name: str, budget: Budget,
                 output_dir: str | Path, seed: int, indexes=None) -> None:
        self.replay_name = replay_name
        self.describe_name = describe_name
        self.budget = budget
        self.output_dir = Path(output_dir)
        self.seed = seed
        # Optional explicit list of generator indexes; keeps campaigns focused
        # and is safe across spawned worker boundaries.
        self.indexes = list(indexes) if indexes is not None else None
        self._index_cursor = 0
        self.findings: list[dict] = []
        self._seen: set[str] = set()
        self.next_index = 0
        self.spent = 0
        self.prior_spent = 0
        self.resumed = False

    def run(self) -> SessionCampaignSummary:
        start = time.monotonic()
        self._load_checkpoint()
        generator = SessionSequenceGenerator(self.seed)
        if self.indexes is not None:
            ordered = [i for i in self.indexes if i >= self.next_index]
            cases = (generator.generate(i) for i in ordered)
        else:
            cases = (case for case in generator if case.index >= self.next_index)
        budget_exhausted = self.spent >= self.budget.max_cases

        def budget_left() -> bool:
            return self.spent < self.budget.max_cases and not self._wall_expired(start)

        case_iter = iter(cases)
        while True:
            # Finish confirmation/reduction for findings already discovered.
            if self._has_unfinished_findings():
                if not self._continue_unfinished_findings(start) or not budget_left():
                    budget_exhausted = True
                    break
            if not budget_left():
                budget_exhausted = True
                break
            try:
                case = next(case_iter)
            except StopIteration:
                # No new inputs remain, but in-flight confirmation/reduction
                # still consumes budget and must be driven to completion or
                # to a pausable checkpoint.
                if self._has_unfinished_findings():
                    self._continue_unfinished_findings(start)
                    budget_exhausted = not budget_left() or self._has_unfinished_findings()
                break
            result = self._replay(case, namespace="detect-a")
            if result.outcome not in FAILURE_OUTCOMES:
                # One extra free detection replay surfaces intermittent
                # defects that missed on the first attempt.  Separate
                # namespaces force opposite parity rather than consecutive
                # increments, and the case is still billed once.
                second = self._replay(case, namespace="detect-b")
                if second.outcome in FAILURE_OUTCOMES:
                    result = second
            self.spent += 1
            self.next_index = case.index + 1
            if result.outcome in FAILURE_OUTCOMES:
                signature = signature_from_result(result)
                if signature.key() not in self._seen:
                    self._begin_finding(case, signature)
            self._write_checkpoint()

        duration = time.monotonic() - start
        summary = SessionCampaignSummary(
            self.seed,
            self.replay_name,
            self.prior_spent + self.spent,
            len(self.findings),
            str(self.output_dir),
            duration,
            budget_exhausted,
            self.resumed,
        )
        self._write_summary(summary)
        return summary



    # -- replay boundary ---------------------------------------------------

    def _replay(self, sequence: SessionSequence, namespace: str = "confirm") -> ReplayResult:
        # Always a fresh process: no worker can carry polluted state forward.
        return replay_sequence_isolated(
            sequence,
            self.replay_name,
            self.describe_name,
            self.budget.timeout,
            attempt_namespace=namespace,
        )

    def _wall_expired(self, start: float) -> bool:
        return self.budget.wall_time is not None and time.monotonic() - start >= self.budget.wall_time

    # -- finding lifecycle -------------------------------------------------

    def _begin_finding(self, sequence: SessionSequence, signature: FailureSignature) -> None:
        stem = self._stem(signature, sequence.index)
        sequence.save(self.output_dir / f"{stem}.original.sequence.json")
        # The raw original is also the first replayable minimum candidate.
        sequence.save(self.output_dir / f"{stem}.minimized.sequence.json")
        finding = {
            "key": signature.key(),
            "stem": stem,
            "original_index": sequence.index,
            "stage": "confirming",
            "signature": self._signature_dict(signature),
            "confirmation": {"hits": 0, "runs": 0, "reproduction_rate": 0.0, "trials": []},
            "reduction": None,
            "stable": False,
        }
        self.findings.append(finding)
        self._seen.add(signature.key())
        self._save_finding(finding)
        self._write_checkpoint()

    def _has_unfinished_findings(self) -> bool:
        return any(item["stage"] in ("confirming", "reducing") for item in self.findings)

    def _continue_unfinished_findings(self, start: float) -> bool:
        """Resume confirmation/reduction that a budget pause interrupted."""
        for finding in list(self.findings):
            while finding["stage"] in ("confirming", "reducing"):
                if self.spent >= self.budget.max_cases or self._wall_expired(start):
                    return False
                if finding["stage"] == "confirming":
                    if not self._confirm_step(finding):
                        break
                else:
                    if not self._reduce_step(finding):
                        break
        return True

    def _confirm_step(self, finding: dict) -> bool:
        signature = self._signature_from_finding(finding)
        repeats = CONFIRM_TIMEOUT_REPEATS if signature.outcome is Outcome.TIMEOUT else CONFIRM_REPEATS
        sequence = SessionSequence.load(self.output_dir / f"{finding['stem']}.original.sequence.json")
        evidence = finding["confirmation"]
        attempt_number = evidence["runs"]
        result = self._replay(sequence, namespace=f"confirm-{attempt_number}")
        self.spent += 1
        evidence["trials"].append(result.to_dict())
        evidence["runs"] = len(evidence["trials"])
        evidence["hits"] = sum(1 for trial in evidence["trials"] if self._trial_matches(trial, signature))
        evidence["reproduction_rate"] = evidence["hits"] / evidence["runs"]
        if evidence["runs"] >= repeats:
            finding["stable"] = evidence["hits"] / evidence["runs"] >= CONFIRM_THRESHOLD
            if finding["stable"]:
                finding["stage"] = "reducing"
                finding["reduction"] = None
            else:
                # Weak/intermittent: keep the raw original and all hit evidence.
                finding["stage"] = "kept_unstable"
            self._save_finding(finding)
            self._write_checkpoint()
            return False
        self._save_finding(finding)
        self._write_checkpoint()
        return True

    def _reduce_step(self, finding: dict) -> bool:
        signature = self._signature_from_finding(finding)
        original = SessionSequence.load(self.output_dir / f"{finding['stem']}.original.sequence.json")
        prior = finding.get("reduction")
        report = SequenceReductionReport.from_checkpoint(prior) if prior else None
        remaining = max(0, self.budget.max_cases - self.spent)
        report, consumed, exhausted = reduce_sequence(
            original,
            signature,
            self._replay,
            repeats=REDUCE_REPEATS,
            budget=remaining,
            spent=0,
            report=report,
        )
        self.spent += consumed
        # Persist the current best minimized candidate independently.
        report.minimized.save(self.output_dir / f"{finding['stem']}.minimized.sequence.json")
        finding["reduction"] = report.to_checkpoint()
        finding["stage"] = "reduced" if report.complete else "reducing"
        finding["stable"] = True
        self._save_finding(finding)
        self._write_checkpoint()
        return report.complete and not exhausted

    # -- helpers -----------------------------------------------------------

    def _trial_matches(self, trial: dict, signature: FailureSignature) -> bool:
        if trial["outcome"] != signature.outcome.value:
            return False
        if signature.outcome is Outcome.CRASH and trial.get("exception_type") != signature.exception_type:
            return False
        if signature.fault_line and trial.get("fault_line") != signature.fault_line:
            return False
        if signature.state_path and trial.get("state_path") != signature.state_path:
            return False
        return True

    def _stem(self, signature: FailureSignature, index: int) -> str:
        return f"{signature.key().replace('/', '_')}#{index}"

    def _signature_dict(self, signature: FailureSignature) -> dict:
        return {
            "outcome": signature.outcome.value,
            "exception_type": signature.exception_type,
            "message": signature.message,
            "fault_file": signature.fault_file,
            "fault_function": signature.fault_function,
            "fault_line": signature.fault_line,
            "state_path": signature.state_path,
            "step_key_conditions": [
                {"step": step, "offset": offset, "value": value}
                for step, offset, value in signature.step_key_conditions
            ],
            "key": signature.key(),
        }

    def _signature_from_finding(self, finding: dict) -> FailureSignature:
        payload = finding["signature"]
        return FailureSignature(
            Outcome(payload["outcome"]),
            payload.get("exception_type", ""),
            payload.get("message", ""),
            payload.get("fault_file", ""),
            payload.get("fault_function", ""),
            payload.get("fault_line", 0),
            state_path=payload.get("state_path", ""),
            step_key_conditions=tuple(
                (int(item["step"]), int(item["offset"]), int(item["value"]))
                for item in payload.get("step_key_conditions", [])
            ),
        )

    def _finding_path(self, finding: dict) -> Path:
        return self.output_dir / f"{finding['stem']}.json"

    def _save_finding(self, finding: dict) -> None:
        _atomic_json(self._finding_path(finding), finding)

    def _checkpoint_path(self) -> Path:
        return self.output_dir / CHECKPOINT_NAME

    def _write_checkpoint(self) -> None:
        payload = {
            "seed": self.seed,
            "replay": self.replay_name,
            "describe": self.describe_name,
            "budget": asdict(self.budget),
            "next_index": self.next_index,
            "replays": self.prior_spent + self.spent,
            "indexes": self.indexes,
            "seen_keys": sorted(self._seen),
            "findings": [
                {"key": item["key"], "stem": item["stem"], "stage": item["stage"],
                 "original_index": item["original_index"]}
                for item in self.findings
            ],
        }
        _atomic_json(self._checkpoint_path(), payload)

    def _load_checkpoint(self) -> None:
        path = self._checkpoint_path()
        if not path.exists():
            return
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.next_index = int(payload.get("next_index", 0))
        self.prior_spent = int(payload.get("replays", 0))
        self.resumed = True
        for item in payload.get("findings", []):
            self._seen.add(item["key"])
            record_path = self.output_dir / f"{item['stem']}.json"
            if record_path.exists():
                self.findings.append(json.loads(record_path.read_text(encoding="utf-8")))

    def _write_summary(self, summary: SessionCampaignSummary) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "seed": summary.seed,
            "target": summary.target,
            "replays": summary.replays,
            "findings": summary.findings,
            "duration": summary.duration,
            "budget": asdict(self.budget),
            "budget_exhausted": summary.budget_exhausted,
            "resumed": summary.resumed,
            "next_index": self.next_index,
            "finding_files": [f"{item['stem']}.json" for item in self.findings],
        }
        _atomic_json(self.output_dir / "session.campaign.json", payload)
