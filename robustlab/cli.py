"""命令行入口。

示例::

    python -m robustlab fuzz --seed 20260929 --iterations 300 --name demo
    python -m robustlab replay results/demo/failures/crash/001__*/minimized.bin
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from robustlab.campaign import Campaign, CampaignConfig
from robustlab.runner import SubprocessRunner


def _cmd_fuzz(args: argparse.Namespace) -> int:
    overrides = {
        "name": args.name,
        "seed": args.seed,
        "max_iterations": args.iterations,
        "max_wall_time_s": args.max_time,
        "timeout_s": args.timeout,
        "results_root": args.results_root,
    }
    overrides = {k: v for k, v in overrides.items() if v is not None}
    if args.goal:
        config = CampaignConfig.from_file(args.goal)
        for key, value in overrides.items():
            setattr(config, key, value)
        config.stop_after_first_failure |= args.stop_on_first
        if args.no_minimize:
            config.minimize = False
    else:
        config = CampaignConfig(
            stop_after_first_failure=args.stop_on_first,
            minimize=not args.no_minimize,
            **overrides,
        )
    campaign = Campaign(config)
    report = campaign.run()
    print(f"实验目录: {report.store.dir}")
    print("结果计数（accepted/rejected/crash/timeout 分开统计）:")
    for kind, count in sorted(report.counts.items()):
        print(f"  {kind:9s} {count}")
    if not report.findings:
        print("未发现崩溃或超时。")
        return 0
    print(f"\n发现 {len(report.findings)} 种独立失败：")
    for finding in report.findings:
        line = (
            f"  [{finding['kind'].upper():7s}] {finding['fingerprint']}  "
            f"{finding.get('original_size')} -> {finding.get('minimized_size', '?')} 字节"
        )
        print(line)
        suffix = "/minimized.bin" if "minimized_size" in finding else "/original.bin"
        print(f"      {finding['path']}{suffix}")
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    runner = SubprocessRunner(timeout_s=args.timeout)
    data = Path(args.sample).read_bytes()
    result = runner.run(data)
    print(f"sample: {args.sample} ({len(data)} 字节)")
    print(f"kind   : {result.kind.value}")
    print(f"exit   : {result.exit_code}  耗时 {result.duration_s:.3f}s")
    if result.reason:
        print(f"reason : {result.reason}")
    if result.stderr.strip():
        print("--- stderr ---")
        print(result.stderr.strip())
    return 0 if not result.kind.is_failure else 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="robustlab", description="协议健壮性研究系统")
    sub = parser.add_subparsers(dest="command", required=True)

    fuzz = sub.add_parser("fuzz", help="对目标执行受控变异测试")
    fuzz.add_argument("--seed", type=int, default=None, help="固定随机种子")
    fuzz.add_argument("--iterations", type=int, default=None)
    fuzz.add_argument("--max-time", type=float, default=None)
    fuzz.add_argument("--timeout", type=float, default=None, help="单个样本截止时间(秒)")
    fuzz.add_argument("--name", default=None)
    fuzz.add_argument("--results-root", default=None)
    fuzz.add_argument("--stop-on-first", action="store_true")
    fuzz.add_argument("--no-minimize", action="store_true")
    fuzz.add_argument("--goal", help="从 goals/ 下的 JSON 目标文件加载配置")
    fuzz.set_defaults(func=_cmd_fuzz)

    replay = sub.add_parser("replay", help="重放一个样本文件")
    replay.add_argument("sample")
    replay.add_argument("--timeout", type=float, default=1.0)
    replay.set_defaults(func=_cmd_replay)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
