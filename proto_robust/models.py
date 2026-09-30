from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import time


class Outcome(str, Enum):
    OK = "ok"
    REJECTED = "rejected"
    CRASH = "crash"
    TIMEOUT = "timeout"
    RUNNER_ERROR = "runner_error"


class ParseReject(Exception):
    """A documented, non-robustness-failure parser rejection."""


@dataclass(frozen=True)
class FailureSignature:
    outcome: Outcome
    exception_type: str = ""
    message: str = ""
    site: str = ""
    # A compact fingerprint of the key trigger condition, e.g. the out-of
    # range index for an IndexError. Preserved during reduction so one crash
    # at the same site cannot silently become a simpler one.
    condition: str = ""

    def key(self) -> str:
        if self.outcome is Outcome.CRASH and self.exception_type:
            # Two distinct defects can raise the very same exception type.
            # The raise site inside the target keeps them in separate groups.
            core = f"crash:{self.exception_type}@{self.site}" if self.site else f"crash:{self.exception_type}"
            return f"{core}#{self.condition}" if self.condition else core
        if self.outcome is Outcome.TIMEOUT:
            return "timeout"
        if self.outcome is Outcome.REJECTED:
            return "rejected"
        return self.outcome.value


@dataclass
class TargetResult:
    outcome: Outcome
    duration: float
    exception_type: str = ""
    message: str = ""
    exit_code: int | None = None
    site: str = ""
    condition: str = ""

    @property
    def signature(self) -> FailureSignature:
        return FailureSignature(
            self.outcome,
            self.exception_type,
            self.message,
            self.site,
            self.condition,
        )


@dataclass(frozen=True)
class Budget:
    max_cases: int = 1000
    timeout: float = 1.0
    wall_time: float | None = None
    # Total isolated executions, counting stability probes and reduction
    # trials in addition to fuzz cases. None means unlimited.
    max_executions: int | None = None


class BudgetExhausted(Exception):
    """Raised inside an execution callback once the execution budget is spent."""


@dataclass(frozen=True)
class ReproEvidence:
    """Repeated-trial evidence for how reliably a failure reproduces."""

    hits: int
    trials: int
    outcomes: list[str] = field(default_factory=list)

    @property
    def stable(self) -> bool:
        return self.trials > 0 and self.hits == self.trials

    @property
    def rate(self) -> float:
        return self.hits / self.trials if self.trials else 0.0


@dataclass
class ReductionStep:
    """One reduction move and why it was accepted or rejected."""

    phase: str
    action: str
    accepted: bool
    reason: str
    candidate_hex: str
    hits: int = 0
    trials: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "action": self.action,
            "accepted": self.accepted,
            "reason": self.reason,
            "candidate_hex": self.candidate_hex,
            "hits": self.hits,
            "trials": self.trials,
        }


@dataclass
class Finding:
    signature: FailureSignature
    original: bytes
    minimized: bytes
    original_index: int
    attempts: int
    critical_byte_offsets: list[int] = field(default_factory=list)
    repro: ReproEvidence | None = None
    reduction_steps: list[ReductionStep] = field(default_factory=list)
    reduction_done: bool = False
    generated_label: str = ""
    created_at: float = field(default_factory=time.time)

    def save(self, directory: str | Path) -> Path:
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        safe_key = _safe_filename(self.signature.key())
        (path / f"{safe_key}.original.bin").write_bytes(self.original)
        (path / f"{safe_key}.minimized.bin").write_bytes(self.minimized)
        (path / f"{safe_key}.json").write_text(self.to_json(), encoding="utf-8")
        return path

    def to_json(self) -> str:
        import json

        return json.dumps(
            {
                "signature": {
                    "outcome": self.signature.outcome.value,
                    "exception_type": self.signature.exception_type,
                    "message": self.signature.message,
                    "site": self.signature.site,
                    "condition": self.signature.condition,
                    "key": self.signature.key(),
                },
                "original_index": self.original_index,
                "generated_label": self.generated_label,
                "attempts": self.attempts,
                "created_at": self.created_at,
                "original_hex": self.original.hex(),
                "minimized_hex": self.minimized.hex(),
                "minimized_size": len(self.minimized),
                "critical_byte_offsets": self.critical_byte_offsets,
                "reduction_done": self.reduction_done,
                "repro": None
                if self.repro is None
                else {
                    "hits": self.repro.hits,
                    "trials": self.repro.trials,
                    "rate": self.repro.rate,
                    "stable": self.repro.stable,
                    "outcomes": self.repro.outcomes,
                },
                "reduction_steps": [step.to_dict() for step in self.reduction_steps],
            },
            ensure_ascii=False,
            indent=2,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, object], directory: str | Path) -> "Finding":
        signature_payload = payload["signature"]
        signature = FailureSignature(
            Outcome(signature_payload["outcome"]),
            signature_payload.get("exception_type", ""),
            signature_payload.get("message", ""),
            signature_payload.get("site", ""),
            signature_payload.get("condition", ""),
        )
        repro_payload = payload.get("repro")
        repro = None
        if repro_payload:
            repro = ReproEvidence(
                repro_payload["hits"],
                repro_payload["trials"],
                list(repro_payload.get("outcomes", [])),
            )
        steps = [
            ReductionStep(
                step["phase"],
                step["action"],
                step["accepted"],
                step["reason"],
                step["candidate_hex"],
                step.get("hits", 0),
                step.get("trials", 0),
            )
            for step in payload.get("reduction_steps", [])
        ]
        return cls(
            signature,
            bytes.fromhex(payload["original_hex"]),
            bytes.fromhex(payload["minimized_hex"]),
            payload["original_index"],
            payload.get("attempts", 0),
            list(payload.get("critical_byte_offsets", [])),
            repro,
            steps,
            bool(payload.get("reduction_done", False)),
            payload.get("generated_label", ""),
            payload.get("created_at", time.time()),
        )

    @classmethod
    def load(cls, directory: str | Path, signature: FailureSignature) -> "Finding | None":
        path = Path(directory) / f"{_safe_filename(signature.key())}.json"
        if not path.exists():
            return None
        import json

        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")), directory)


def _safe_filename(value: str) -> str:
    for separator in ("/", "\\"):
        value = value.replace(separator, "_")
    return value
