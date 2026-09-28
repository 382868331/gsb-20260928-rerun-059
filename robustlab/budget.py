"""运行预算管理：时间、次数、首次发现后是否继续，均可复用。"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class Budget:
    max_iterations: int = 2000
    max_wall_time_s: float = 120.0
    stop_after_first_failure: bool = False
    found: int = 0
    iterations: int = 0
    _start: float = 0.0

    def start(self) -> None:
        self._start = time.monotonic()

    def tick(self, new_failure: bool = False) -> None:
        self.iterations += 1
        if new_failure:
            self.found += 1

    @property
    def elapsed_s(self) -> float:
        return max(0.0, time.monotonic() - self._start) if self._start else 0.0

    @property
    def exhausted(self) -> bool:
        if self.iterations >= self.max_iterations:
            return True
        if self.elapsed_s >= self.max_wall_time_s:
            return True
        if self.stop_after_first_failure and self.found:
            return True
        return False

    def status(self) -> dict:
        return {
            "iterations": self.iterations,
            "max_iterations": self.max_iterations,
            "elapsed_s": round(self.elapsed_s, 3),
            "max_wall_time_s": self.max_wall_time_s,
            "found": self.found,
        }
