"""CLI for versioned experiments that join radio and orbit analysis."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from leo_tracker.orbit import Observer
from leo_tracker.radio.beacon.replay import (
    DEFAULT_REPLAY_ID, create_replay_plan, replay_status, run_replay)


def _plan(args: argparse.Namespace) -> int:
    report = create_replay_plan(
        args.root, replay_id=args.replay_id,
        maximum_gap_s=args.maximum_gap_s,
        maximum_reacquisition_span_hz=args.maximum_reacquisition_span_hz,
        minimum_dual_observations=args.minimum_dual_observations)
    print(json.dumps({"replay_id": report["replay_id"],
                      "job_count": report["job_count"],
                      "excluded": report["excluded"],
                      "plan": str(Path(args.root).resolve() / "reports" / "replays" /
                                  args.replay_id / "plan.json")}, sort_keys=True))
    return 0


def _run(args: argparse.Namespace) -> int:
    report = run_replay(
        args.root, replay_id=args.replay_id, workers=args.workers,
        observer=Observer(args.lat, args.lon, args.alt_m), limit=args.limit,
        names=args.name,
        progress=lambda item: print(json.dumps(
            {"event": "replay_progress", **item}, sort_keys=True), flush=True))
    print(json.dumps(report, sort_keys=True))
    return 0


def _status(args: argparse.Namespace) -> int:
    print(json.dumps(replay_status(args.root, replay_id=args.replay_id), sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="leo-replay")
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="immutably plan high-value track replays")
    plan.add_argument("root", type=Path)
    plan.add_argument("--replay-id", default=DEFAULT_REPLAY_ID)
    plan.add_argument("--maximum-gap-s", type=float, default=15)
    plan.add_argument("--maximum-reacquisition-span-hz", type=float, default=15_000)
    plan.add_argument("--minimum-dual-observations", type=int, default=45)
    plan.set_defaults(handler=_plan)
    run = commands.add_parser("run", help="resume track and TLE association replay")
    run.add_argument("root", type=Path)
    run.add_argument("--replay-id", default=DEFAULT_REPLAY_ID)
    run.add_argument("--workers", type=int, default=1)
    run.add_argument("--limit", type=int)
    run.add_argument("--name", action="append",
                     help="run only this planned recording; repeat as needed")
    run.add_argument("--lat", type=float, default=37.849165355010086)
    run.add_argument("--lon", type=float, default=-122.48567658142287)
    run.add_argument("--alt-m", type=float, default=0)
    run.set_defaults(handler=_run)
    status = commands.add_parser("status", help="report replay progress and identities")
    status.add_argument("root", type=Path)
    status.add_argument("--replay-id", default=DEFAULT_REPLAY_ID)
    status.set_defaults(handler=_status)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return int(args.handler(args))
    except (ValueError, RuntimeError, FileNotFoundError, ImportError) as error:
        print(f"leo-replay: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
