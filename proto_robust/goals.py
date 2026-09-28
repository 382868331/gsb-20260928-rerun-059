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
