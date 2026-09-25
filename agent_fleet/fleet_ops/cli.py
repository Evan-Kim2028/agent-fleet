"""CLI registration for the multi-operator lane manager.

Adds two subcommands to the top-level parser:

* ``agent-fleet lane run …``     — drive one lane end to end
* ``agent-fleet lanes status|stop`` — cross-operator view and precise stop

Registered via :func:`register_lane_commands`, mirroring
``workstreams.cli.register_workstream_commands``, so ``cli.py`` only gains a
two-line import and call and the argparse surface stays in one place.

The gate capability check is resolved here, from the live ``sub.choices``, so
``lane run`` learns whether a gate exists at parse time rather than probing the
shell later.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from agent_fleet.fleet_ops.models import ModelPolicyError
from agent_fleet.fleet_ops.runner import run_lane
from agent_fleet.fleet_ops.status import render_table, status_dicts, status_rows
from agent_fleet.fleet_ops.stop import stop_lane_by_name

if TYPE_CHECKING:
    import argparse


def cmd_lane_run(args: argparse.Namespace) -> int:
    """Run one lane: implement, guarantee a PR, gate, write the status line."""
    known = getattr(args, "_known_subcommands", None)
    try:
        result = run_lane(
            operator=args.operator,
            lane=args.lane,
            repo_path=Path(args.repo_path).expanduser(),
            task_file=Path(args.task_file).expanduser(),
            engine=args.engine,
            branch=args.branch,
            status_file=Path(args.status_file).expanduser() if args.status_file else None,
            expected_slug=args.expected_repo,
            worktree_parent=Path(args.worktree_parent).expanduser()
            if args.worktree_parent
            else None,
            known_gate_subcommands=known,
            gate=not args.no_gate,
        )
    except ModelPolicyError as exc:
        print(f"error: model policy: {exc}", file=sys.stderr)
        return 2
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
    else:
        print(f"lane {result.lane} [{result.operator}] -> {result.state}")
        if result.pr:
            print(f"  PR: #{result.pr} ({result.branch} @ {(result.head or '')[:9]})")
        if result.status_line:
            print(f"  status: {result.status_line}")
        if result.detail:
            print(f"  detail: {result.detail[:500]}")

    return 0 if result.state != "escalated" else 1


def cmd_lanes_status(args: argparse.Namespace) -> int:
    """Show every lane, or one operator's lanes."""
    if args.operator and args.all:
        print("error: pass either --operator or --all, not both", file=sys.stderr)
        return 2

    operator = None if args.all or not args.operator else args.operator
    if args.json:
        print(json.dumps(status_dicts(operator=operator), indent=2, default=str))
    else:
        title = "all operators" if operator is None else f"operator {operator}"
        print(render_table(status_rows(operator=operator), title=f"lanes ({title})"))
    return 0


def cmd_lanes_stop(args: argparse.Namespace) -> int:
    """Stop exactly one lane's process group."""
    result = stop_lane_by_name(args.lane, operator=args.operator, grace_s=args.grace)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
    elif result.stopped:
        print(f"stopped {args.lane} ({result.reason})")
    else:
        print(f"refused to stop {args.lane}: {result.reason}", file=sys.stderr)
        if result.detail:
            print(f"  {result.detail}", file=sys.stderr)
    return 0 if result.stopped else 1


def register_lane_commands(sub: argparse._SubParsersAction) -> None:
    """Register ``lane`` and ``lanes`` on the top-level parser."""
    # Live mapping, not a snapshot: the gate subcommand is registered later in main().
    gate_known = sub.choices

    lane_p = sub.add_parser(
        "lane",
        help="Run one coding lane end to end (worktree, implement, guarantee PR, gate)",
    )
    lane_sub = lane_p.add_subparsers(dest="lane_command", required=True)

    run_p = lane_sub.add_parser("run", help="Run a lane and guarantee it produces a PR")
    run_p.add_argument(
        "--operator", required=True, help="Operator session name (e.g. documents-0e)"
    )
    run_p.add_argument("--lane", required=True, help="Lane name; branch defaults to fb/<lane>")
    run_p.add_argument("--repo-path", required=True, help="Path to the repository")
    run_p.add_argument("--task-file", required=True, help="Task file for the implementer")
    run_p.add_argument(
        "--engine",
        choices=("cmd", "devin"),
        default=None,
        help="Implementation engine (default: the operator's configured engine)",
    )
    run_p.add_argument("--branch", default=None, help="Branch override (default: fb/<lane>)")
    run_p.add_argument(
        "--worktree-parent",
        default=None,
        help="Directory to hold lane worktrees (default: ~/Documents, the bash drivers' layout)",
    )
    run_p.add_argument(
        "--expected-repo",
        default=None,
        metavar="OWNER/REPO",
        help=(
            "Assert the lane's origin is this slug; the run escalates instead of "
            "gating if the worktree is in a different repo (guards against a "
            "review pointing at another team's PR)"
        ),
    )
    run_p.add_argument(
        "--status-file",
        default=None,
        help="Append the final status line here (automerge tails this file)",
    )
    run_p.add_argument(
        "--no-gate",
        action="store_true",
        help="Stop after the PR is guaranteed; an external gate reviews it (status: GATE-SKIPPED)",
    )
    run_p.add_argument("--json", action="store_true", help="Emit the full result as JSON")
    run_p.set_defaults(func=cmd_lane_run, _known_subcommands=gate_known)

    lanes_p = sub.add_parser(
        "lanes",
        help="Inspect and stop lanes across operators",
    )
    lanes_sub = lanes_p.add_subparsers(dest="lanes_command", required=True)

    status_p = lanes_sub.add_parser("status", help="Table of lanes, states, and PRs")
    status_p.add_argument("--operator", default=None, help="Only this operator's lanes")
    status_p.add_argument("--all", action="store_true", help="All operators (the default)")
    status_p.add_argument("--json", action="store_true", help="Emit rows as JSON")
    status_p.set_defaults(func=cmd_lanes_status)

    stop_p = lanes_sub.add_parser("stop", help="Stop one lane's process group")
    stop_p.add_argument("lane", help="Lane name")
    stop_p.add_argument(
        "--operator", default=None, help="Disambiguate when two operators share a lane name"
    )
    stop_p.add_argument(
        "--grace", type=float, default=10.0, help="Seconds between SIGTERM and SIGKILL"
    )
    stop_p.add_argument("--json", action="store_true", help="Emit the result as JSON")
    stop_p.set_defaults(func=cmd_lanes_stop)
