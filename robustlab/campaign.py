"""实验编排：生成 -> 子进程执行 -> 分类去重 -> 保存 -> 缩小。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from robustlab.budget import Budget
from robustlab.generator import SeededGenerator
from robustlab.minimizer import SameFailureMinimizer
from robustlab.outcome import ExecutionResult, Kind
from robustlab.runner import SubprocessRunner
from robustlab.store import ResultStore


@dataclass
class CampaignConfig:
    name: str = "message-parser"
    seed: int = 20260929
    max_iterations: int = 1000
    max_wall_time_s: float = 60.0
    timeout_s: float = 1.0
    stop_after_first_failure: bool = False
    minimize: bool = True
    results_root: str = "results"
    # 目标注册表：以后接入新协议时，只需要提供一个新的 worker 模块名。
    target: str = "robustlab.worker"

    @classmethod
    def from_file(cls, path: str | Path) -> "CampaignConfig":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        allowed = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in allowed})


@dataclass
class CampaignReport:
    store: ResultStore
    counts: dict = field(default_factory=dict)
    findings: list[dict] = field(default_factory=list)


class Campaign:
    def __init__(self, config: CampaignConfig, runner: SubprocessRunner | None = None) -> None:
        self.config = config
        self.generator = SeededGenerator(config.seed)
        self.budget = Budget(
            max_iterations=config.max_iterations,
            max_wall_time_s=config.max_wall_time_s,
            stop_after_first_failure=config.stop_after_first_failure,
        )
        self.runner = runner or SubprocessRunner(timeout_s=config.timeout_s)
        self.store = ResultStore(Path(config.results_root), config.name)
        self.counts = {k.value: 0 for k in Kind}

    def run(self) -> CampaignReport:
        self.budget.start()
        minimizer = SameFailureMinimizer(self.runner)
        for index, sample in enumerate(self.generator.stream(self.config.max_iterations)):
            result = self.runner.run(sample)
            is_new_failure = False
            self.counts[result.kind.value] = self.counts.get(result.kind.value, 0) + 1
            sig = result.signature()
            if sig is not None and not self.store.is_duplicate(sig):
                finding_dir = self.store.save_failure(sample, result, sig, index)
                is_new_failure = True
                if self.config.minimize:
                    outcome = minimizer.minimize(sample, sig)
                    self.store.record_minimized(
                        finding_dir, outcome.sample, outcome.executions
                    )
            self.budget.tick(new_failure=is_new_failure)
            if self.budget.exhausted:
                break
        self.store.write_manifest(
            config={
                "seed": self.config.seed,
                "timeout_s": self.config.timeout_s,
                "target": self.config.target,
                "stop_after_first_failure": self.config.stop_after_first_failure,
            },
            budget_status=self.budget.status(),
            counts=self.counts,
        )
        return CampaignReport(
            store=self.store,
            counts=self.counts,
            findings=self.store.list_findings(),
        )
