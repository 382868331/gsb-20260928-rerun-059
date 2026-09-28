"""实验结果管理：目标/输入/结果三层分离中的“结果”层。

目录布局::

  results/<campaign>/
    manifest.json            实验配置、预算、按类别的统计
    failures/
      crash/<n>__<fingerprint>/
        original.bin
        minimized.bin
        finding.json
      timeout/<n>__deadline-exceeded/
      rejected/...           (默认不保存；可用 --keep-rejected 留存)
    corpus/                  新发现样本回流（可选）
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from robustlab.outcome import ExecutionResult, FailureSignature, Kind


class ResultStore:
    def __init__(self, root: Path, campaign: str | None = None) -> None:
        self.root = Path(root)
        self.campaign = campaign or time.strftime("campaign-%Y%m%d-%H%M%S")
        self.dir = self.root / self.campaign
        self.failures_dir = self.dir / "failures"
        for kind in ("crash", "timeout", "rejected"):
            (self.failures_dir / kind).mkdir(parents=True, exist_ok=True)
        (self.dir / "corpus").mkdir(parents=True, exist_ok=True)
        self._known: dict[str, set[str]] = {k.value: set() for k in Kind}
        self._counters = {"crash": 0, "timeout": 0, "rejected": 0}

    def is_duplicate(self, sig: FailureSignature) -> bool:
        bucket = self._known[sig.kind.value]
        digest = hashlib.sha256(sig.fingerprint.encode()).hexdigest()
        return digest in bucket

    def save_failure(
        self,
        sample: bytes,
        result: ExecutionResult,
        sig: FailureSignature,
        iteration: int,
    ) -> Path:
        """崩溃/超时进程结束后立即落盘保存“当前样本”。返回发现目录。"""
        kind_name = sig.kind.value
        self._counters[kind_name] = self._counters.get(kind_name, 0) + 1
        digest = hashlib.sha256(sig.fingerprint.encode()).hexdigest()
        safe_fp = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in sig.fingerprint)
        name = f"{self._counters[kind_name]:03d}__{safe_fp[:48] or digest}"
        finding_dir = self.failures_dir / kind_name / name
        finding_dir.mkdir(parents=True, exist_ok=True)
        (finding_dir / "original.bin").write_bytes(sample)
        finding = {
            "kind": kind_name,
            "fingerprint": sig.fingerprint,
            "first_seen_iteration": iteration,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "original_size": len(sample),
            "exit_code": result.exit_code,
            "duration_s": round(result.duration_s, 4),
            "reason": result.reason,
            "sha256": hashlib.sha256(sample).hexdigest(),
        }
        (finding_dir / "finding.json").write_text(
            json.dumps(finding, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._known[kind_name].add(digest)
        return finding_dir

    def record_minimized(self, finding_dir: Path, minimized: bytes, executions: int) -> None:
        (finding_dir / "minimized.bin").write_bytes(minimized)
        finding_path = finding_dir / "finding.json"
        finding = json.loads(finding_path.read_text(encoding="utf-8"))
        finding["minimized_size"] = len(minimized)
        finding["minimization_executions"] = executions
        finding["minimized_sha256"] = hashlib.sha256(minimized).hexdigest()
        finding_path.write_text(
            json.dumps(finding, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def write_manifest(self, config: dict, budget_status: dict, counts: dict) -> None:
        manifest = {
            "campaign": self.campaign,
            "config": config,
            "budget": budget_status,
            "counts": counts,
            "findings": self.list_findings(),
        }
        (self.dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def list_findings(self) -> list[dict]:
        items = []
        for kind_dir in self.failures_dir.iterdir():
            if not kind_dir.is_dir():
                continue
            for finding_dir in sorted(kind_dir.iterdir()):
                finding_file = finding_dir / "finding.json"
                if finding_file.exists():
                    meta = json.loads(finding_file.read_text(encoding="utf-8"))
                    meta["path"] = str(finding_dir)
                    items.append(meta)
        return items
