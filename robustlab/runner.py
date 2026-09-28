"""沙箱执行器：每个样本在独立子进程中解析。

* 超时会 ``kill`` 整个进程组并回收（``communicate`` 等待回收）；
* 子进程意外死亡（信号、负退出码）也会保存当前样本到失败目录；
* accepted / rejected / crash / timeout 四类结果互不混淆。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from robustlab.outcome import ExecutionResult, Kind


class SubprocessRunner:
    def __init__(
        self,
        timeout_s: float = 2.0,
        python_executable: str | None = None,
        cwd: Path | None = None,
        cleanup: bool = True,
    ) -> None:
        self.timeout_s = timeout_s
        self._python = python_executable or sys.executable
        self._cwd = cwd or Path.cwd()
        self._cleanup = cleanup

    def _execute(self, sample_path: Path) -> ExecutionResult:
        start = time.monotonic()
        proc = subprocess.Popen(
            [self._python, "-m", "robustlab.worker", str(sample_path)],
            cwd=self._cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout_b, stderr_b = proc.communicate(timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            self._kill_process_tree(proc)
            try:
                stdout_b, stderr_b = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - 极端回收失败
                proc.kill()
                stdout_b, stderr_b = proc.communicate()
            duration = time.monotonic() - start
            return ExecutionResult(
                kind=Kind.TIMEOUT,
                exit_code=proc.returncode,
                duration_s=duration,
                stdout=stdout_b.decode(errors="replace"),
                stderr=stderr_b.decode(errors="replace"),
                reason=f"deadline {self.timeout_s}s exceeded",
                sample_path=str(sample_path),
            )

        duration = time.monotonic() - start
        stdout = stdout_b.decode(errors="replace")
        stderr = stderr_b.decode(errors="replace")
        code = proc.returncode
        if code == 0:
            kind, reason = Kind.ACCEPTED, None
        elif code == 2:
            kind, reason = Kind.REJECTED, stderr.strip()[:200] or None
        else:
            kind, reason = Kind.CRASH, f"worker exited with code {code}"
        return ExecutionResult(
            kind=kind,
            exit_code=code,
            duration_s=duration,
            stdout=stdout,
            stderr=stderr,
            reason=reason,
            sample_path=str(sample_path),
        )

    def run(self, sample: bytes) -> ExecutionResult:
        """执行样本。调用方负责把样本落盘；这里从内存写到临时文件，
        保证即使进程死亡样本也已保存。"""
        import tempfile

        sample_dir = Path(tempfile.mkdtemp(prefix="robustlab_run_", dir=self._cwd))
        sample_path = sample_dir / "current_sample.bin"
        sample_path.write_bytes(sample)  # 先落盘再执行，进程死亡也不丢样本

        try:
            return self._execute(sample_path)
        finally:
            # 失败样本在分类后会复制到 findings 目录，这里只清理执行临时区。
            if self._cleanup:
                import shutil

                shutil.rmtree(sample_dir, ignore_errors=True)

    @staticmethod
    def _kill_process_tree(proc: subprocess.Popen) -> None:
        try:
            import signal

            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
