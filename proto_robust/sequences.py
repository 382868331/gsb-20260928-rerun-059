"""Multi-step session input sequences and full interaction transcripts.

A research input is an ordered list of interactions replayed from a clean
session starting point.  Sequences are JSON-serializable so that both the
original failing trace and every minimized candidate remain independently
replayable after a budget pause.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Interaction:
    name: str
    data: bytes

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "hex": self.data.hex()}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Interaction":
        return cls(str(payload["name"]), bytes.fromhex(payload.get("hex", "")))


@dataclass(frozen=True)
class SessionSequence:
    index: int
    label: str
    start_state: str
    steps: tuple[Interaction, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "label": self.label,
            "start_state": self.start_state,
            "steps": [step.to_dict() for step in self.steps],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SessionSequence":
        return cls(
            int(payload["index"]),
            str(payload.get("label", "")),
            str(payload.get("start_state", "closed")),
            tuple(Interaction.from_dict(step) for step in payload.get("steps", [])),
        )

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return target

    @classmethod
    def load(cls, path: str | Path) -> "SessionSequence":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


@dataclass
class StepRecord:
    """Outcome of one interaction inside a single replay attempt."""

    step: int
    name: str
    hex: str
    outcome: str
    exception_type: str = ""
    message: str = ""
    state_before: str = ""
    state_after: str = ""
    duration: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "name": self.name,
            "hex": self.hex,
            "outcome": self.outcome,
            "exception_type": self.exception_type,
            "message": self.message,
            "state_before": self.state_before,
            "state_after": self.state_after,
            "duration": self.duration,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "StepRecord":
        return cls(
            step=int(payload["step"]),
            name=str(payload["name"]),
            hex=str(payload["hex"]),
            outcome=str(payload["outcome"]),
            exception_type=str(payload.get("exception_type", "")),
            message=str(payload.get("message", "")),
            state_before=str(payload.get("state_before", "")),
            state_after=str(payload.get("state_after", "")),
            duration=float(payload.get("duration", 0.0)),
        )


@dataclass
class ReplayResult:
    """A full replay of one sequence from its session starting point."""

    outcome: str
    start_state: str
    end_state: str
    duration: float
    exception_type: str = ""
    message: str = ""
    fault_file: str = ""
    fault_function: str = ""
    fault_line: int = 0
    state_path: str = ""
    key_conditions: tuple[tuple[int, int, int], ...] = field(default_factory=tuple)
    failing_step: int = -1
    exit_code: int | None = None
    steps: list[StepRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "start_state": self.start_state,
            "end_state": self.end_state,
            "duration": self.duration,
            "exception_type": self.exception_type,
            "message": self.message,
            "fault_file": self.fault_file,
            "fault_function": self.fault_function,
            "fault_line": self.fault_line,
            "state_path": self.state_path,
            "key_conditions": [
                {"step": step, "offset": offset, "value": value}
                for step, offset, value in self.key_conditions
            ],
            "failing_step": self.failing_step,
            "exit_code": self.exit_code,
            "steps": [record.to_dict() for record in self.steps],
        }
