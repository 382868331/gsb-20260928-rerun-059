from __future__ import annotations

import argparse

from proto_robust.fuzzer import FuzzingCampaign
from proto_robust.goals import ResearchGoal
from proto_robust.models import Budget

DEFAULT_TARGET = "targets.simple_message.replay_session"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a deterministic local protocol robustness campaign")
    parser.add_argument("--goal", help="JSON research goal; target and budget are read from it unless overridden")
    parser.add_argument("--target", help="Dotted Python parser callable")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--cases", type=int)
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--wall-time", type=float)
    parser.add_argument("--output", default="results/latest")
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore an existing campaign checkpoint in the output directory",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    goal = ResearchGoal.load(args.goal) if args.goal else None
    target = args.target or (goal.target if goal else DEFAULT_TARGET)
    if goal and not goal.local_only:
        raise SystemExit("this research build refuses non-local goals")
    seed = args.seed if args.seed is not None else 20260929
    cases = args.cases if args.cases is not None else 128
    timeout = args.timeout if args.timeout is not None else 1.0
    campaign = FuzzingCampaign(
        target,
        Budget(cases, timeout, args.wall_time),
        args.output,
        seed,
    )
    if args.fresh:
        campaign.ignore_checkpoint = True
        campaign.next_index = 0
        campaign.prior_executed = 0
        campaign.resumed = False
        campaign._seen.clear()
        campaign.findings.clear()
    summary = campaign.run()
    print(
        f"seed={summary.seed} cases={summary.cases_executed} "
        f"findings={summary.findings} output={summary.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
