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
    PREPARATION_ERROR = "preparation_error"


class ParseReject(Exception):
    """A documented, non-robustness-failure parser rejection."""


class SessionPreparationError(Exception):
    """The environment or session could not be prepared for a comparison."""


@dataclass(frozen=True)
class FailureSignature:
    outcome: Outcome
    exception_type: str = ""
    message: str = ""
    fault_file: str = ""
    fault_function: str = ""
    fault_line: int = 0
    key_conditions: tuple[tuple[int, int], ...] = field(default_factory=tuple)
    # Stateful sessions: the session state path that must be active for the
    # defect to fire, plus structural (step_index, offset, value) key bytes.
    state_path: str = ""
    step_key_conditions: tuple[tuple[int, int, int], ...] = field(default_factory=tuple)

    def key(self) -> str:
        if self.outcome is Outcome.CRASH and self.exception_type:
            location = f"@{self.fault_function}:{self.fault_line}" if self.fault_function and self.fault_line else ""
            state = f"#{self.state_path}" if self.state_path else ""
            return f"crash:{self.exception_type}{location}{state}"
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
    key_conditions: tuple[tuple[int, int], ...] = field(default_factory=tuple)
    state_path: str = ""
    step_key_conditions: tuple[tuple[int, int, int], ...] = field(default_factory=tuple)

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
            self.state_path,
            tuple(self.step_key_conditions),
        )


@dataclass(frozen=True)
class Budget:
    max_cases: int = 1000
    timeout: float = 1.0
    wall_time: float | None = None


@dataclass
class Finding:
    signature: FailureSignature
    original: bytes
    minimized: bytes
    original_index: int
    attempts: int
    critical_byte_offsets: list[int] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    reduction_steps: list[dict] = field(default_factory=list)
    confirmation: dict = field(default_factory=dict)
    stable: bool = True

    def save(self, directory: str | Path) -> Path:
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        safe_key = self.signature.key().replace("/", "_")
        stem = path / f"{safe_key}#{self.original_index}"
        stem.with_suffix(".original.bin").write_bytes(self.original)
        stem.with_suffix(".minimized.bin").write_bytes(self.minimized)
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
                        {"offset": offset, "value": value}
                        for offset, value in self.signature.key_conditions
                    ],
                    "key": self.signature.key(),
                },
                "original_index": self.original_index,
                "attempts": self.attempts,
                "stable": self.stable,
                "confirmation": self.confirmation,
                "created_at": self.created_at,
                "original_hex": self.original.hex(),
                "minimized_hex": self.minimized.hex(),
                "minimized_size": len(self.minimized),
                "critical_byte_offsets": self.critical_byte_offsets,
                "reduction_steps": self.reduction_steps,
            },
            ensure_ascii=False,
            indent=2,
        )
