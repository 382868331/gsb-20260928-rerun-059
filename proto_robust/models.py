from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import json
import time


class Outcome(str, Enum):
    OK = "ok"
    REJECTED = "rejected"
    CRASH = "crash"
    TIMEOUT = "timeout"
    RUNNER_ERROR = "runner_error"


@dataclass(frozen=True)
class SessionStep:
    data: bytes
    label: str = "message"


@dataclass(frozen=True)
class SessionSequence:
    """A complete replay: a fresh session followed by ordered interactions."""

    steps: tuple[SessionStep, ...]

    @property
    def size(self) -> int:
        return sum(len(step.data) for step in self.steps)

    @property
    def interaction_count(self) -> int:
        return len(self.steps)

    def flattened(self) -> bytes:
        return b"".join(step.data for step in self.steps)

    def to_dict(self) -> dict:
        return {
            "format": "session-sequence/v1",
            "fresh_session": True,
            "interaction_count": len(self.steps),
            "size": self.size,
            "steps": [
                {"index": index, "label": step.label, "hex": step.data.hex()}
                for index, step in enumerate(self.steps)
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, payload: dict) -> "SessionSequence":
        steps = tuple(
            SessionStep(
                bytes.fromhex(str(step["hex"])),
                str(step.get("label", "message")),
            )
            for step in payload.get("steps", [])
        )
        return cls(steps)

    @classmethod
    def from_bytes(cls, data: bytes, label: str = "message") -> "SessionSequence":
        return cls((SessionStep(bytes(data), label),))


class ParseReject(Exception):
    """A documented, non-robustness-failure parser rejection."""


@dataclass(frozen=True)
class FailureSignature:
    outcome: Outcome
    exception_type: str = ""
    message: str = ""
    fault_file: str = ""
    fault_function: str = ""
    fault_line: int = 0
    # Conditions are (step_index, byte_offset, expected_value). Step -1 means
    # the final interaction and keeps the representation useful for one-message
    # cases while reducers verify it against the concrete final index.
    key_conditions: tuple[tuple[int, int, int], ...] = field(default_factory=tuple)

    def key(self) -> str:
        if self.outcome is Outcome.CRASH and self.exception_type:
            if self.fault_function and self.fault_line:
                return f"crash:{self.exception_type}@{self.fault_function}:{self.fault_line}"
            return f"crash:{self.exception_type}"
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
    fault_file: str = ""
    fault_function: str = ""
    fault_line: int = 0
    key_conditions: tuple[tuple[int, int, int], ...] = field(default_factory=tuple)

    @property
    def signature(self) -> FailureSignature:
        return FailureSignature(
            self.outcome,
            self.exception_type,
            self.message,
            self.fault_file,
            self.fault_function,
            self.fault_line,
            tuple(self.key_conditions),
        )


@dataclass(frozen=True)
class Budget:
    max_cases: int = 1000
    timeout: float = 1.0
    wall_time: float | None = None


@dataclass
class Finding:
    signature: FailureSignature
    original: SessionSequence
    minimized: SessionSequence
    original_index: int
    attempts: int
    critical_positions: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    reduction_steps: list[dict] = field(default_factory=list)
    confirmation: dict = field(default_factory=dict)
    stable: bool = True
    reduction_completed: bool = True

    def save(self, directory: str | Path) -> Path:
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        safe_key = self.signature.key().replace("/", "_")
        stem = path / f"{safe_key}#{self.original_index}"
        stem.with_suffix(".original.session.json").write_text(self.original.to_json(), encoding="utf-8")
        stem.with_suffix(".minimized.session.json").write_text(self.minimized.to_json(), encoding="utf-8")
        # Backwards-compatible raw artifacts; the session JSON is authoritative
        # because byte concatenation cannot recover interaction boundaries.
        stem.with_suffix(".original.bin").write_bytes(self.original.flattened())
        stem.with_suffix(".minimized.bin").write_bytes(self.minimized.flattened())
        stem.with_suffix(".json").write_text(self.to_json(), encoding="utf-8")
        return path

    @property
    def file_stem(self) -> str:
        return f"{self.signature.key().replace('/', '_')}#{self.original_index}"

    def to_json(self) -> str:
        import json

        return json.dumps(
            {
                "signature": {
                    "outcome": self.signature.outcome.value,
                    "exception_type": self.signature.exception_type,
                    "message": self.signature.message,
                    "fault_file": self.signature.fault_file,
                    "fault_function": self.signature.fault_function,
                    "fault_line": self.signature.fault_line,
                    "key_conditions": [
                        {"step": step, "offset": offset, "value": value}
                        for step, offset, value in self.signature.key_conditions
                    ],
                    "key": self.signature.key(),
                },
                "original_index": self.original_index,
                "attempts": self.attempts,
                "stable": self.stable,
                "confirmation": self.confirmation,
                "created_at": self.created_at,
                "original_sequence": self.original.to_dict(),
                "minimized_sequence": self.minimized.to_dict(),
                "original_hex": self.original.flattened().hex(),
                "minimized_hex": self.minimized.flattened().hex(),
                "minimized_size": self.minimized.size,
                "critical_positions": self.critical_positions,
                "reduction_completed": self.reduction_completed,
                "reduction_steps": self.reduction_steps,
            },
            ensure_ascii=False,
            indent=2,
        )
