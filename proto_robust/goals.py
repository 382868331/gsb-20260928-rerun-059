from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path


@dataclass(frozen=True)
class ResearchGoal:
    name: str
    target: str
    description: str
    local_only: bool = True

    @classmethod
    def load(cls, path: str | Path) -> "ResearchGoal":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            name=payload["name"],
            target=payload["target"],
            description=payload.get("description", ""),
            local_only=payload.get("local_only", True),
        )


@dataclass(frozen=True)
class CompareGoal:
    """Two-version comparison goal: old/new target identities and policy."""

    name: str
    old: dict
    new: dict
    description: str = ""
    local_only: bool = True
    valid_attempts: int = 3
    stop_on_first_hit: bool = True

    @classmethod
    def load(cls, path: str | Path) -> "CompareGoal":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        policy = payload.get("compare_policy", {})
        return cls(
            name=payload["name"],
            old=payload["old"],
            new=payload["new"],
            description=payload.get("description", ""),
            local_only=payload.get("local_only", True),
            valid_attempts=int(policy.get("valid_attempts", 3)),
            stop_on_first_hit=bool(policy.get("stop_on_first_hit", True)),
        )
