"""Two-version comparison of the same fault sequences.

One generated fault sequence is replayed from a clean session start against
two target builds (old / new).  Every round keeps, per side, the session
starting point, the ordered inputs, the target identity and the actual
result.  Attempts that could not finish (worker/environment failure, or
simply never run because the budget paused) are recorded as incomplete and
are never counted as misses.

Classification per candidate:

* fixed_in_new       -- the old build reproduces the failure while the new
  build shows no failure in any completed attempt;
* different_location -- both builds fail, but at different defect
  locations (file/function/line or state path);
* same_location      -- both builds fail with the same signature;
* new_only           -- only the new build observes a failure;
* incomparable       -- environment or session preparation did not
  complete on at least one side (e.g. a build-up interaction is rejected or
  the worker cannot finish), so the trigger is never reached;
* unstable           -- the failing side does not reproduce reliably.

Reduction keeps the researcher-selected version-difference condition: a
candidate is accepted only while the failing side still reproduces its
signature *and* the other side still satisfies the condition (no failure
observed, or a failure at its own distinct location).

The campaign checkpoints after every replay; a budget pause resumes without
re-counting finished attempts.  Group reruns (the campaign) and single
reruns (the compare-rerun CLI) append to the same per-side trial evidence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile
import time

from proto_robust.compare_reducer import DifferenceReductionReport, reduce_difference
from proto_robust.generator import SessionSequenceGenerator
from proto_robust.models import Budget, FailureSignature, Outcome
from proto_robust.session_campaign import signature_from_result
from proto_robust.session_reducer import same_failure
from proto_robust.session_runner import replay_sequence_isolated
from proto_robust.sequences import ReplayResult, SessionSequence

FAILURE_OUTCOMES = {Outcome.CRASH.value, Outcome.TIMEOUT.value, Outcome.RUNNER_ERROR.value}
CHECKPOINT_NAME = "compare.campaign.checkpoint.json"
SUMMARY_NAME = "compare.campaign.json"
CONFIRM_THRESHOLD = 0.5
MAX_INCOMPLETE_STREAK = 3


@dataclass(frozen=True)
class CompareSide:
    name: str
    replay: str
    describe: str


@dataclass(frozen=True)
class ComparePolicy:
    # Per-candidate stop conditions: how many completed (valid) attempts each
    # side needs, and whether a side may stop as soon as it observes a hit.
    valid_attempts: int = 3
    stop_on_first_hit: bool = True
    reduce_repeats: int = 2


@dataclass
class CompareSummary:
    seed: int
    replays: int
    candidates: int
    classifications: dict
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


def _sig_dict(signature: FailureSignature) -> dict:
    return {
        "outcome": signature.outcome.value,
        "exception_type": signature.exception_type,
        "message": signature.message,
        "fault_file": signature.fault_file,
        "fault_function": signature.fault_function,
        "fault_line": signature.fault_line,
        "state_path": signature.state_path,
        "key": signature.key(),
    }


def _sig_from_dict(payload: dict) -> FailureSignature:
    return FailureSignature(
        Outcome(payload["outcome"]),
        payload.get("exception_type", ""),
        payload.get("message", ""),
        payload.get("fault_file", ""),
        payload.get("fault_function", ""),
        payload.get("fault_line", 0),
        state_path=payload.get("state_path", ""),
    )


def _disposition(result: ReplayResult, signature: FailureSignature | None, trigger_step: int) -> str:
    failing = result.failing_step if result.failing_step >= 0 else trigger_step
    if result.outcome == Outcome.OK.value:
        return "miss"
    if failing < trigger_step:
        # Session build-up broke before the trigger interaction was reached.
        return "prep_failed"
    if result.outcome == Outcome.REJECTED.value:
        return "rejected"
    if result.outcome in FAILURE_OUTCOMES:
        if signature is not None and same_failure(result, signature):
            return "hit"
        return "observed_failure"
    return "other"


def _side_record(side: CompareSide, signature: FailureSignature | None) -> dict:
    return {
        "identity": side.name,
        "replay": side.replay,
        "describe": side.describe,
        "signature": _sig_dict(signature) if signature else None,
        "trials": [],
        "hits": 0,
        "observed": 0,
        "valid_runs": 0,
        "incomplete_streak": 0,
        "prep_blocked": False,
        "dispositions": {},
    }


def record_trial(candidate: dict, side_name: str, result: ReplayResult | None,
                 source: str, completed: bool = True, note: str = "",
                 trigger_step: int | None = None) -> dict:
    """Append one attempt; unfinished attempts never count as misses."""
    side = candidate["sides"][side_name]
    attempt = len(side["trials"])
    if trigger_step is None:
        trigger_step = candidate["trigger_step"]
    if not completed or result is None:
        side["trials"].append({
            "attempt": attempt,
            "source": source,
            "side": side_name,
            "target": side["replay"],
            "start_state": candidate.get("start_state", "closed"),
            "outcome": "incomplete",
            "disposition": "incomplete",
            "completed": False,
            "note": note,
            "steps": [],
        })
        side["incomplete_streak"] = side.get("incomplete_streak", 0) + 1
        if side["incomplete_streak"] >= MAX_INCOMPLETE_STREAK:
            # The environment cannot finish a replay on this side.
            side["prep_blocked"] = True
        return side["trials"][-1]
    signature = _sig_from_dict(side["signature"]) if side.get("signature") else None
    disposition = _disposition(result, signature, trigger_step)
    trial = {
        "attempt": attempt,
        "source": source,
        "side": side_name,
        "target": side["replay"],
        "start_state": result.start_state,
        "outcome": result.outcome,
        "exception_type": result.exception_type,
        "fault_file": result.fault_file,
        "fault_function": result.fault_function,
        "fault_line": result.fault_line,
        "state_path": result.state_path,
        "failing_step": result.failing_step,
        "trigger_step": trigger_step,
        "disposition": disposition,
        "completed": True,
        "steps": [step.to_dict() for step in result.steps],
    }
    side["trials"].append(trial)
    side["incomplete_streak"] = 0
    side["valid_runs"] += 1
    side["dispositions"][disposition] = side["dispositions"].get(disposition, 0) + 1
    if disposition == "hit":
        side["hits"] += 1
        side["observed"] += 1
    elif disposition == "observed_failure":
        side["observed"] += 1
        if side["signature"] is None:
            side["signature"] = _sig_dict(signature_from_result(result))
    elif disposition == "prep_failed":
        side["prep_blocked"] = True
    return trial


def side_decided(side: dict, policy: ComparePolicy) -> bool:
    if side.get("prep_blocked"):
        return True
    if side["valid_runs"] >= policy.valid_attempts:
        return True
    if policy.stop_on_first_hit and side.get("observed", 0) > 0:
        return True
    return False


def _same_location(first: dict, second: dict) -> bool:
    keys = ("outcome", "exception_type", "fault_file", "fault_function", "fault_line", "state_path")
    return all(first.get(key) == second.get(key) for key in keys)


def classify_candidate(candidate: dict, policy: ComparePolicy) -> bool:
    """Classify once both sides are decided; returns True when classified."""
    old = candidate["sides"]["old"]
    new = candidate["sides"]["new"]
    # Recompute preparation blockage from the recorded trials so that
    # reclassification after extra reruns stays consistent.
    for side in (old, new):
        side["prep_blocked"] = any(
            trial.get("disposition") == "prep_failed" for trial in side["trials"]
        ) or side.get("incomplete_streak", 0) >= MAX_INCOMPLETE_STREAK
    if not (side_decided(old, policy) and side_decided(new, policy)):
        return False
    if old.get("prep_blocked") or new.get("prep_blocked"):
        candidate["classification"] = "incomparable"
        candidate["difference_condition"] = None
        candidate["stage"] = "done"
        return True
    old_repro = bool(old["valid_runs"]) and old["hits"] / old["valid_runs"] >= CONFIRM_THRESHOLD
    new_repro = bool(new["valid_runs"]) and new["observed"] / new["valid_runs"] >= CONFIRM_THRESHOLD
    old_obs = old.get("observed", 0) > 0
    new_obs = new.get("observed", 0) > 0
    condition = None
    if old_repro and not new_obs:
        classification = "fixed_in_new"
        condition = {
            "kind": "not_observed",
            "failing_side": "old",
            "failing_signature": old["signature"],
            "other_signature": None,
        }
    elif new_repro and not old_obs:
        classification = "new_only"
        condition = {
            "kind": "not_observed",
            "failing_side": "new",
            "failing_signature": new["signature"],
            "other_signature": None,
        }
    elif old_obs and new_obs:
        if _same_location(old["signature"], new["signature"]):
            classification = "same_location"
        else:
            classification = "different_location"
            failing_side = "old" if old_repro else "new"
            failing = candidate["sides"][failing_side]
            other = candidate["sides"]["new" if failing_side == "old" else "old"]
            condition = {
                "kind": "different_location",
                "failing_side": failing_side,
                "failing_signature": failing["signature"],
                "other_signature": other["signature"],
            }
    else:
        classification = "unstable"
    candidate["classification"] = classification
    candidate["difference_condition"] = condition
    reduction = candidate.get("reduction")
    if condition is not None and not (reduction and reduction.get("complete")):
        candidate["stage"] = "reducing"
    else:
        candidate["stage"] = "done"
    return True


class CompareCampaign:
    """Replay generated fault sequences against two target builds."""

    def __init__(self, old: CompareSide, new: CompareSide, budget: Budget,
                 output_dir: str | Path, seed: int, policy: ComparePolicy | None = None,
                 indexes=None) -> None:
        self.sides = {"old": old, "new": new}
        self.budget = budget
        self.output_dir = Path(output_dir)
        self.seed = seed
        self.policy = policy or ComparePolicy()
        self.indexes = list(indexes) if indexes is not None else None
        self.candidates: list[dict] = []
        self.next_index = 0
        self.spent = 0
        self.prior_spent = 0
        self.resumed = False

    # -- main loop ---------------------------------------------------------

    def run(self) -> CompareSummary:
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
            # Finish confirmation/reduction for candidates already discovered.
            if self._has_unfinished():
                if not self._continue_unfinished(start) or not budget_left():
                    budget_exhausted = True
                    break
            if not budget_left():
                budget_exhausted = True
                break
            try:
                case = next(case_iter)
            except StopIteration:
                if self._has_unfinished():
                    self._continue_unfinished(start)
                    budget_exhausted = not budget_left() or self._has_unfinished()
                break
            # Detection for one case is atomic w.r.t. checkpoints, so a
            # resume never double-counts or re-runs finished attempts.
            self._detect(case)
            self.next_index = case.index + 1
            self._write_checkpoint()

        duration = time.monotonic() - start
        classifications: dict[str, int] = {}
        for candidate in self.candidates:
            key = candidate["classification"]
            classifications[key] = classifications.get(key, 0) + 1
        summary = CompareSummary(
            self.seed,
            self.prior_spent + self.spent,
            len(self.candidates),
            classifications,
            str(self.output_dir),
            duration,
            budget_exhausted,
            self.resumed,
        )
        self._write_summary(summary)
        return summary

    # -- detection ---------------------------------------------------------

    def _detect_side(self, side_name: str, case: SessionSequence) -> tuple[list, object]:
        side = self.sides[side_name]
        first = self._replay(side, case, "detect-a")
        results = [first]
        chosen = first
        if first.outcome not in FAILURE_OUTCOMES:
            # One extra free detection replay with opposite parity, so an
            # intermittent defect is not missed on the first attempt.
            second = self._replay(side, case, "detect-b")
            results.append(second)
            if second.outcome in FAILURE_OUTCOMES:
                chosen = second
        return results, chosen

    def _detect(self, case: SessionSequence) -> None:
        old_results, old_chosen = self._detect_side("old", case)
        new_results, new_chosen = self._detect_side("new", case)
        self.spent += len(old_results) + len(new_results)
        if (old_chosen.outcome not in FAILURE_OUTCOMES
                and new_chosen.outcome not in FAILURE_OUTCOMES):
            return
        self._begin_candidate(case, old_results, old_chosen, new_results, new_chosen)

    def _begin_candidate(self, case: SessionSequence, old_results, old_chosen,
                         new_results, new_chosen) -> None:
        if old_chosen.outcome in FAILURE_OUTCOMES and old_chosen.failing_step >= 0:
            trigger = old_chosen.failing_step
        elif new_chosen.failing_step >= 0:
            trigger = new_chosen.failing_step
        else:
            trigger = len(case.steps) - 1
        stem = f"candidate#{case.index}"
        case.save(self.output_dir / f"{stem}.original.sequence.json")
        case.save(self.output_dir / f"{stem}.minimized.sequence.json")
        old_sig = (signature_from_result(old_chosen)
                   if old_chosen.outcome in FAILURE_OUTCOMES else None)
        new_sig = (signature_from_result(new_chosen)
                   if new_chosen.outcome in FAILURE_OUTCOMES else None)
        candidate = {
            "stem": stem,
            "index": case.index,
            "label": case.label,
            "stage": "confirming",
            "classification": "pending",
            "difference_condition": None,
            "trigger_step": trigger,
            "start_state": case.start_state,
            "timeout": self.budget.timeout,
            "stop_condition": {
                "valid_attempts": self.policy.valid_attempts,
                "stop_on_first_hit": self.policy.stop_on_first_hit,
            },
            "sides": {
                "old": _side_record(self.sides["old"], old_sig),
                "new": _side_record(self.sides["new"], new_sig),
            },
            "reduction": None,
        }
        for result in old_results:
            record_trial(candidate, "old", result, source="campaign")
        for result in new_results:
            record_trial(candidate, "new", result, source="campaign")
        classify_candidate(candidate, self.policy)
        self.candidates.append(candidate)
        self._save_candidate(candidate)
        self._write_checkpoint()

    # -- replay boundary ---------------------------------------------------

    def _replay(self, side: CompareSide, sequence: SessionSequence, namespace: str) -> ReplayResult:
        # Always a fresh process: no worker can carry polluted state forward.
        return replay_sequence_isolated(
            sequence, side.replay, side.describe, self.budget.timeout,
            attempt_namespace=namespace,
        )

    def _wall_expired(self, start: float) -> bool:
        return self.budget.wall_time is not None and time.monotonic() - start >= self.budget.wall_time

    # -- candidate lifecycle -------------------------------------------------

    def _has_unfinished(self) -> bool:
        return any(item["stage"] in ("confirming", "reducing") for item in self.candidates)

    def _continue_unfinished(self, start: float) -> bool:
        for candidate in list(self.candidates):
            while candidate["stage"] in ("confirming", "reducing"):
                if self.spent >= self.budget.max_cases or self._wall_expired(start):
                    return False
                if candidate["stage"] == "confirming":
                    if not self._confirm_step(candidate):
                        break
                else:
                    if not self._reduce_step(candidate):
                        break
        return True

    def _confirm_step(self, candidate: dict) -> bool:
        for side_name in ("old", "new"):
            side = candidate["sides"][side_name]
            if side_decided(side, self.policy):
                continue
            attempt = len(side["trials"])
            sequence = SessionSequence.load(
                self.output_dir / f"{candidate['stem']}.original.sequence.json")
            try:
                result = self._replay(self.sides[side_name], sequence, f"confirm-{attempt}")
            except Exception as exc:  # environment failure: not a miss
                record_trial(candidate, side_name, None, source="campaign",
                             completed=False, note=str(exc))
            else:
                self.spent += 1
                record_trial(candidate, side_name, result, source="campaign")
            self._save_candidate(candidate)
            self._write_checkpoint()
            return True
        classify_candidate(candidate, self.policy)
        self._save_candidate(candidate)
        self._write_checkpoint()
        return candidate["stage"] in ("confirming", "reducing")

    def _reduce_step(self, candidate: dict) -> bool:
        condition = candidate["difference_condition"]
        failing = condition["failing_side"]
        other = "new" if failing == "old" else "old"
        original = SessionSequence.load(
            self.output_dir / f"{candidate['stem']}.original.sequence.json")
        prior = candidate.get("reduction")
        report = DifferenceReductionReport.from_checkpoint(prior) if prior else None
        remaining = max(0, self.budget.max_cases - self.spent)
        report, consumed, exhausted = reduce_difference(
            original,
            _sig_from_dict(condition["failing_signature"]),
            condition,
            lambda seq: self._replay(self.sides[failing], seq, "reduce"),
            lambda seq: self._replay(self.sides[other], seq, "reduce"),
            other_signature=(_sig_from_dict(condition["other_signature"])
                             if condition.get("other_signature") else None),
            repeats=self.policy.reduce_repeats,
            budget=remaining,
            spent=0,
            report=report,
        )
        self.spent += consumed
        # Persist the current best minimized candidate independently; it stays
        # replayable under both builds' original session conditions.
        report.minimized.save(self.output_dir / f"{candidate['stem']}.minimized.sequence.json")
        candidate["reduction"] = report.to_checkpoint()
        if report.complete:
            candidate["stage"] = "done"
        self._save_candidate(candidate)
        self._write_checkpoint()
        return report.complete and not exhausted

    # -- persistence ---------------------------------------------------------

    def _candidate_path(self, candidate: dict) -> Path:
        return self.output_dir / f"{candidate['stem']}.json"

    def _save_candidate(self, candidate: dict) -> None:
        _atomic_json(self._candidate_path(candidate), candidate)

    def _checkpoint_path(self) -> Path:
        return self.output_dir / CHECKPOINT_NAME

    def _write_checkpoint(self) -> None:
        payload = {
            "seed": self.seed,
            "sides": {name: asdict(side) for name, side in self.sides.items()},
            "budget": asdict(self.budget),
            "policy": asdict(self.policy),
            "next_index": self.next_index,
            "replays": self.prior_spent + self.spent,
            "indexes": self.indexes,
            "candidates": [
                {"stem": item["stem"], "stage": item["stage"],
                 "classification": item["classification"], "index": item["index"]}
                for item in self.candidates
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
        for item in payload.get("candidates", []):
            record_path = self.output_dir / f"{item['stem']}.json"
            if record_path.exists():
                self.candidates.append(json.loads(record_path.read_text(encoding="utf-8")))

    def _write_summary(self, summary: CompareSummary) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "seed": summary.seed,
            "sides": {name: asdict(side) for name, side in self.sides.items()},
            "replays": summary.replays,
            "candidates": summary.candidates,
            "classifications": summary.classifications,
            "duration": summary.duration,
            "budget": asdict(self.budget),
            "policy": asdict(self.policy),
            "budget_exhausted": summary.budget_exhausted,
            "resumed": summary.resumed,
            "next_index": self.next_index,
            "candidate_files": [f"{item['stem']}.json" for item in self.candidates],
            "dispositions": {
                item["stem"]: {
                    name: dict(side["dispositions"])
                    for name, side in item["sides"].items()
                }
                for item in self.candidates
            },
        }
        _atomic_json(self.output_dir / SUMMARY_NAME, payload)


def rerun_candidate(candidate_file: str | Path, side_name: str | None = None,
                    attempts: int = 1, timeout: float | None = None,
                    source: str = "rerun-single") -> dict:
    """Append independent rerun attempts to one saved candidate.

    The candidate's current minimized sequence is replayed under each side's
    original session conditions (same target identity, same clean start).
    Results are appended to the same trial evidence the group campaign
    produced, and the classification is recomputed from all completed
    attempts.
    """
    path = Path(candidate_file)
    candidate = json.loads(path.read_text(encoding="utf-8"))
    stop = candidate.get("stop_condition", {})
    policy = ComparePolicy(
        valid_attempts=int(stop.get("valid_attempts", 3)),
        stop_on_first_hit=bool(stop.get("stop_on_first_hit", True)),
    )
    stem = candidate["stem"]
    sequence_path = path.parent / f"{stem}.minimized.sequence.json"
    if not sequence_path.exists():
        sequence_path = path.parent / f"{stem}.original.sequence.json"
    sequence = SessionSequence.load(sequence_path)
    # The trigger interaction may sit at an earlier position in a reduced
    # sequence; locate it by name so prep failures are still judged against
    # the real trigger, not the original index.
    trigger_step = candidate["trigger_step"]
    original_path = path.parent / f"{stem}.original.sequence.json"
    if original_path.exists():
        original = SessionSequence.load(original_path)
        if 0 <= trigger_step < len(original.steps):
            trigger_name = original.steps[trigger_step].name
            matches = [i for i, step in enumerate(sequence.steps) if step.name == trigger_name]
            if matches:
                trigger_step = matches[-1]
    trigger_step = min(trigger_step, max(0, len(sequence.steps) - 1))
    effective_timeout = timeout if timeout is not None else float(candidate.get("timeout", 1.0))
    names = [side_name] if side_name else ["old", "new"]
    for name in names:
        side_info = candidate["sides"][name]
        side = CompareSide(side_info["identity"], side_info["replay"], side_info["describe"])
        for _ in range(max(1, attempts)):
            attempt = len(side_info["trials"])
            try:
                result = replay_sequence_isolated(
                    sequence, side.replay, side.describe, effective_timeout,
                    attempt_namespace=f"rerun-{attempt}",
                )
            except Exception as exc:  # environment failure: not a miss
                record_trial(candidate, name, None, source=source,
                             completed=False, note=str(exc))
            else:
                record_trial(candidate, name, result, source=source, trigger_step=trigger_step)
    classify_candidate(candidate, policy)
    _atomic_json(path, candidate)
    return candidate
