from __future__ import annotations

import argparse

from proto_robust.differential_campaign import DifferentialCampaign, condition_from_old_result
from proto_robust.differential import VersionSpec
from proto_robust.fuzzer import FuzzingCampaign
from proto_robust.goals import ResearchGoal
from proto_robust.models import Budget
from proto_robust.paired_campaign import PairedReplayCampaign, load_paired_goal
from proto_robust.session_campaign import signature_from_result
from proto_robust.session_campaign import SessionCampaign
from proto_robust.session_runner import replay_sequence_isolated
from proto_robust.sequences import SessionSequence

DEFAULT_TARGET = "targets.simple_message.parse_message"
DEFAULT_SESSION_REPLAY = "targets.session_store.replay"
DEFAULT_SESSION_DESCRIBE = "targets.session_store.describe_state"
DEFAULT_SHARED_REPLAY = "targets.shared_file_store.replay"
DEFAULT_SHARED_NEW_REPLAY = "targets.shared_file_store_new.replay"
DEFAULT_SHARED_DESCRIBE = "targets.shared_file_store.describe_state"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic local protocol robustness research")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run a single-message (stateless) robustness campaign")
    _add_campaign_args(run_parser)
    session = subparsers.add_parser("session", help="Run a multi-step stateful session campaign")
    _add_campaign_args(session)
    session.add_argument("--replay", default=DEFAULT_SESSION_REPLAY,
                        help="Callable applied to each frame with the session dict")
    session.add_argument("--describe", default=DEFAULT_SESSION_DESCRIBE,
                        help="Callable returning the current session state label")
    session.add_argument("--indexes", default="",
                        help="Comma-separated generator indexes to limit the campaign")

    replay = subparsers.add_parser("replay", help="Replay one saved sequence file independently")
    replay.add_argument("sequence_file")
    replay.add_argument("--replay-target", default=DEFAULT_SESSION_REPLAY)
    replay.add_argument("--describe", default=DEFAULT_SESSION_DESCRIBE)
    replay.add_argument("--timeout", type=float, default=1.0)

    compare = subparsers.add_parser("compare", help="Compare one sequence against old and new targets")
    compare.add_argument("sequence_file")
    compare.add_argument("--old-replay", default=DEFAULT_SESSION_REPLAY)
    compare.add_argument("--new-replay", default="targets.session_store_new.replay")
    compare.add_argument("--describe", default=DEFAULT_SESSION_DESCRIBE)
    compare.add_argument("--old-identity", default="old")
    compare.add_argument("--new-identity", default="new")
    compare.add_argument("--difference", choices=("old_reproducible_new_absent", "different_location"),
                        default="old_reproducible_new_absent")
    compare.add_argument("--order", choices=("fixed", "alternating"), default="fixed",
                        help="Execution order of the two versions inside each paired round")
    _add_campaign_args(compare)

    paired = subparsers.add_parser(
        "paired",
        help="Replay paired groups from frozen, hash-verified shared-preparation snapshots",
    )
    paired.add_argument("--old-replay", default=DEFAULT_SHARED_REPLAY)
    paired.add_argument("--new-replay", default=DEFAULT_SHARED_NEW_REPLAY)
    paired.add_argument("--describe", default=DEFAULT_SHARED_DESCRIBE)
    paired.add_argument("--old-identity", default="old")
    paired.add_argument("--new-identity", default="new")
    paired.add_argument("--no-restore", action="store_true",
                        help="Only restore the frozen preparation before the first side "
                             "of each pair, exposing the second side to pollution")
    _add_campaign_args(paired)

    # Flat (no subcommand) invocation stays equivalent to the stateless run.
    _add_campaign_args(parser)
    return parser


def _add_campaign_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--goal", help="JSON research goal; target and budget default from it unless overridden")
    parser.add_argument("--target", help="Dotted Python parser callable (stateless mode)")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--cases", type=int)
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--wall-time", type=float)
    parser.add_argument("--output", default="results/latest")
    parser.add_argument("--fresh", action="store_true",
                        help="Ignore an existing campaign checkpoint in the output directory")


def _campaign_options(args) -> tuple:
    goal = ResearchGoal.load(args.goal) if args.goal else None
    seed = args.seed if args.seed is not None else 20260929
    cases = args.cases if args.cases is not None else 128
    timeout = args.timeout if args.timeout is not None else 1.0
    return goal, seed, cases, timeout


def _run_stateless(args) -> int:
    goal, seed, cases, timeout = _campaign_options(args)
    target = args.target or (goal.target if goal else DEFAULT_TARGET)
    if goal and not goal.local_only:
        raise SystemExit("this research build refuses non-local goals")
    campaign = FuzzingCampaign(target, Budget(cases, timeout, args.wall_time), args.output, seed)
    if args.fresh:
        campaign.next_index = 0
        campaign.prior_executed = 0
        campaign.resumed = False
        campaign._seen.clear()
        campaign.findings.clear()
    summary = campaign.run()
    print(f"seed={summary.seed} cases={summary.cases_executed} "
          f"findings={summary.findings} output={summary.output_dir}")
    return 0


def _run_session(args) -> int:
    goal, seed, cases, timeout = _campaign_options(args)
    if goal and not goal.local_only:
        raise SystemExit("this research build refuses non-local goals")
    indexes_arg = getattr(args, "indexes", "") or ""
    indexes = [int(part) for part in indexes_arg.split(",") if part.strip()] or None
    campaign = SessionCampaign(
        getattr(args, "replay", DEFAULT_SESSION_REPLAY),
        getattr(args, "describe", DEFAULT_SESSION_DESCRIBE),
        Budget(cases, timeout, args.wall_time),
        args.output,
        seed,
        indexes=indexes,
    )
    if args.fresh:
        campaign.next_index = 0
        campaign.spent = 0
        campaign.prior_spent = 0
        campaign.resumed = False
        campaign._seen.clear()
        campaign.findings.clear()
    summary = campaign.run()
    print(f"seed={summary.seed} replays={summary.replays} "
          f"findings={summary.findings} output={summary.output_dir}")
    return 0


def _replay(args) -> int:
    sequence = SessionSequence.load(args.sequence_file)
    result = replay_sequence_isolated(sequence, args.replay_target, args.describe, args.timeout)
    print(f"start_state={result.start_state} end_state={result.end_state} outcome={result.outcome}")
    for step in result.steps:
        print(f"  #{step.step} {step.name} {step.state_before}->{step.state_after} "
              f"{step.outcome} {step.exception_type}")
    if result.state_path:
        print(f"state_path={result.state_path} fault={result.fault_function}:{result.fault_line}")
    return 0 if result.outcome == "ok" else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "session":
        return _run_session(args)
    if args.command == "replay":
        return _replay(args)
    if args.command == "compare":
        return _compare(args)
    if args.command == "paired":
        return _paired(args)
    return _run_stateless(args)


def _paired(args) -> int:
    _goal, seed, pair_budget, timeout = _campaign_options(args)
    groups, goal_payload = load_paired_goal(args.goal or "goals/shared_file_pairs.json")
    if goal_payload.get("local_only") is False:
        raise SystemExit("this research build refuses non-local goals")
    old = VersionSpec(args.old_identity, args.old_replay, args.describe)
    new = VersionSpec(args.new_identity, args.new_replay, args.describe)
    campaign = PairedReplayCampaign(
        groups, old, new, Budget(pair_budget, timeout, args.wall_time),
        args.output, seed,
        restore_between_sides=not args.no_restore,
        fresh=args.fresh,
    )
    summary = campaign.run()
    totals = summary.totals
    print(f"seed={summary.seed} pairs={summary.pairs_executed} "
          f"hits={totals.get('hits', 0)} prep_failed={totals.get('preparation_failed', 0)} "
          f"timeouts={totals.get('timeout', 0)} output={summary.output_dir}")
    return 0


def _compare(args) -> int:
    _goal, seed, pair_budget, timeout = _campaign_options(args)
    sequence = SessionSequence.load(args.sequence_file)
    old_version = VersionSpec(args.old_identity, args.old_replay, args.describe)
    new_version = VersionSpec(args.new_identity, args.new_replay, args.describe)
    old_result = replay_sequence_isolated(sequence, old_version.replay_name,
                                           old_version.describe_name, timeout,
                                           attempt_namespace="compare-setup-old")
    new_result = replay_sequence_isolated(sequence, new_version.replay_name,
                                          new_version.describe_name, timeout,
                                          attempt_namespace="compare-setup-new")
    new_signature = signature_from_result(new_result) if args.difference == "different_location" else None
    condition = condition_from_old_result(
        old_result, args.difference, old_version, new_version, new_signature
    )
    campaign = DifferentialCampaign(
        sequence, condition, Budget(pair_budget, timeout, args.wall_time),
        args.output, seed, order_mode=args.order, fresh=args.fresh,
    )
    summary = campaign.run()
    print(f"seed={summary.seed} pair_attempts={summary.pair_attempts} "
          f"accepted={summary.accepted_candidates} output={summary.output_dir}")
    return 0 if summary.accepted_candidates else 1


if __name__ == "__main__":
    raise SystemExit(main())
