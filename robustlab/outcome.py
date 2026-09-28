"""失败模式分类。

关键约束：解析拒绝（REJECT）、进程崩溃（CRASH）、超时（TIMEOUT）
是三种互不相同的结果，任何下游逻辑（去重、缩小、统计）都不得合并。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Kind(str, Enum):
    ACCEPTED = "accepted"      # 解析器正常接受输入
    REJECTED = "rejected"      # 解析器明确拒绝（格式/校验错误）
    CRASH = "crash"            # 进程异常退出（未捕获异常、信号等）
    TIMEOUT = "timeout"        # 超过截止时间被强制终止

    @property
    def is_failure(self) -> bool:
        return self in (Kind.CRASH, Kind.TIMEOUT)


@dataclass(frozen=True)
class FailureSignature:
    """用于判定“同一种失败”的身份。

    kind 不同一定不是同一种失败；同 kind 下再比较指纹
    （崩溃用异常类型与栈帧，超时只有一个固定指纹）。
    """

    kind: Kind
    fingerprint: str

    def as_key(self) -> str:
        return f"{self.kind.value}:{self.fingerprint}"


@dataclass
class ExecutionResult:
    kind: Kind
    exit_code: int | None = None
    duration_s: float = 0.0
    stdout: str = ""
    stderr: str = ""
    reason: str | None = None          # 解析拒绝原因
    sample_path: str | None = None     # 本次执行对应的样本文件
    extra: dict = field(default_factory=dict)

    def signature(self) -> FailureSignature | None:
        if self.kind is Kind.CRASH:
            return FailureSignature(Kind.CRASH, _crash_fingerprint(self.stderr))
        if self.kind is Kind.TIMEOUT:
            return FailureSignature(Kind.TIMEOUT, "deadline-exceeded")
        return None


def _crash_fingerprint(stderr: str) -> str:
    """从 Python 子进程的 stderr 中提取稳定的崩溃指纹。

    形如 ``IndexError@_handle_group``；非 Python 信号崩溃则用信号名。
    """
    exc_type = None
    frame = ""
    for line in stderr.splitlines():
        stripped = line.strip()
        if stripped.startswith("File "):
            # File "...", line x, in func
            marker = ", in "
            if marker in stripped:
                frame = stripped.split(marker, 1)[1].strip()
        if stripped and not stripped.startswith(("File ", "Traceback", "During")):
            if ":" in stripped or stripped.endswith("Error") or stripped.endswith("Exception"):
                name = stripped.split(":", 1)[0].strip()
                if name and name[0].isalpha():
                    exc_type = name
    if exc_type:
        return f"{exc_type}@{frame}" if frame else exc_type
    tail = stderr.strip().splitlines()[-1] if stderr.strip() else "unknown"
    return tail[:80]
