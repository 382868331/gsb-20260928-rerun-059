"""子进程入口：读取样本文件并调用目标解析器。

退出码约定（与 robustlab.outcome.Kind 严格对应）：
  0 = accepted；2 = rejected（解析拒绝）；
  1 = crash（未捕获异常，Python traceback 打到 stderr）。
超时不产生退出码——它由执行器 kill 子进程得到。
"""

from __future__ import annotations

import sys

from robustlab.parser import parse_message, ParseError


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: python -m robustlab.worker SAMPLE\n")
        return 1
    with open(argv[1], "rb") as handle:
        data = handle.read()
    try:
        result = parse_message(data)
    except ParseError as exc:
        sys.stderr.write(f"REJECTED: {exc}\n")
        return 2
    # 非 ParseError 的异常故意不捕获：让解释器打印原生 traceback 并以
    # 退出码 1 结束，执行器据此归类为 CRASH（而不是解析拒绝）。
    sys.stdout.write(repr(result) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
