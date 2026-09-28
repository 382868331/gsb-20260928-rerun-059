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

    def key(self) -> str:
        if self.outcome is Outcome.CRASH and self.exception_type:
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

    @property
    def signature(self) -> FailureSignature:
        return FailureSignature(self.outcome, self.exception_type, self.message)


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

    def save(self, directory: str | Path) -> Path:
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        safe_key = self.signature.key().replace("/", "_")
        stem = path / safe_key
        stem.with_suffix(".original.bin").write_bytes(self.original)
        stem.with_suffix(".minimized.bin").write_bytes(self.minimized)
        stem.with_suffix(".json").write_text(self.to_json(), encoding="utf-8")
        return path

    def to_json(self) -> str:
        import json

        return json.dumps(
            {
                "signature": {
                    "outcome": self.signature.outcome.value,
                    "exception_type": self.signature.exception_type,
                    "message": self.signature.message,
                    "key": self.signature.key(),
                },
                "original_index": self.original_index,
                "attempts": self.attempts,
                "created_at": self.created_at,
                "original_hex": self.original.hex(),
                "minimized_hex": self.minimized.hex(),
                "minimized_size": len(self.minimized),
                "critical_byte_offsets": self.critical_byte_offsets,
            },
            ensure_ascii=False,
            indent=2,
        )
