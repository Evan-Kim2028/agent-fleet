"""``agent-fleet route`` and ``agent-fleet pr`` — the post-gate routing commands.

Registered via :func:`register_routing_commands`, mirroring
``merge_plan.cli.register_merge_commands``, so ``cli.py`` only gains an import
and a call and the argparse surface stays in one place.

``route`` is **read-only**: it reads a PR's gate verdict history and the attempt
counters and prints the one action the policy chose, charging nothing and
touching no repository. Deciding and doing are separate commands on purpose —
the policy is worth having in a form an operator can run against a real status
file and compare against what the reconciler did, with no way for that to
launch an agent. ``pr rebase``/``pr repair`` are the commands that do the work.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from agent_fleet.fleet_ops.statusfile import read_status_lines

if TYPE_CHECKING:
    import argparse


def _home(args: argparse.Namespace) -> Path | None:
    value = getattr(args, "ops_home", None)
    return Path(value).expanduser() if value else None


def cmd_route(args: argparse.Namespace) -> int:
    """Print the routing decision for a PR from its verdict history. Never acts."""
    from agent_fleet.routing import read_counters
    from agent_fleet.routing.policy import decide

    if not args.status_file:
        print("error: route requires --status-file (the gate verdict history)", file=sys.stderr)
        return 2
    status = Path(args.status_file).expanduser()
    if not status.exists():
        print(f"error: no status file at {status}", file=sys.stderr)
        return 2

    head = args.head
    lane = args.lane or head.split("/")[-1] or "lane"
    counters = read_counters(lane, head, home=_home(args))
    decision = decide(tuple(read_status_lines(status)), head=head, lane=lane, counters=counters)
    payload = {
        "lane": lane,
        "head": head,
        "status_file": str(status),
        "counters": counters.to_dict(),
        **decision.to_dict(),
    }
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(f"[{lane}] -> {decision.action.value}")
        print(f"  {decision.reason}")
        if decision.exhausted:
            print("  budget spent: this is a human's call now")
    # park is a real answer, not a failure, so it does not set the exit code.
    return 0


def _cmd_pr_action(args: argparse.Namespace, mode: str) -> int:
    from agent_fleet.gate.gitops import resolve_pull_request
    from agent_fleet.routing import Mode, read_counters, record_attempt
    from agent_fleet.routing.executor import resolve_lane, run_agent

    repo = Path(args.repo_path or Path.cwd()).expanduser().resolve()
    home = _home(args)
    ref = resolve_pull_request(repo, args.pr)
    if not ref.is_open:
        print(f"error: PR #{args.pr} is {ref.state or 'not open'}", file=sys.stderr)
        return 1

    head_ref = args.head_ref or ref.head_ref
    lane = resolve_lane(head_ref)
    head = args.head or ref.head_sha or head_ref
    field = "rebase_at_head" if mode == "rebase" else "repair_at_head"
    # The policy's own once-per-head rule, enforced here so the agent is never
    # launched for a budget the decision would have refused to spend.
    if getattr(read_counters(lane, head, home=home), field) >= 1:
        print(
            f"error: {mode} already used on {lane}@{head[:9]}; one per head is the policy",
            file=sys.stderr,
        )
        return 1

    if not args.dry_run:
        # Charged before the action runs, not after: a run the machine drops still
        # cost an agent, and a cap that only counted successes would hand this
        # head an unbounded number of them.
        record_attempt(mode, lane, head, home=home)
    result = run_agent(
        Mode(mode),
        repo_path=repo,
        pr_number=args.pr,
        status_file=Path(args.status_file).expanduser() if args.status_file else None,
        head_ref=head_ref,
        head=head,
    )
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
    elif result.ok:
        print(f"{mode} pushed to {head_ref} @ {result.head[:9]}")
        print(f"  {result.status_line}")
    else:
        print(f"{mode} did not complete: {result.detail}", file=sys.stderr)
    return 0 if result.ok or args.dry_run else 1


def cmd_pr_rebase(args: argparse.Namespace) -> int:
    return _cmd_pr_action(args, "rebase")


def cmd_pr_repair(args: argparse.Namespace) -> int:
    return _cmd_pr_action(args, "repair")


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--status-file",
        default=None,
        help="Gate status file to read (route) or append the re-gate escalation to (pr)",
    )
    p.add_argument(
        "--ops-home",
        default=None,
        help="Attempt-counter directory (default: $FLEET_OPS_HOME)",
    )
    p.add_argument("--json", action="store_true", help="Emit the result as JSON")


def _add_action_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo-path", default=None, help="Checkout holding the PR (default: cwd)")
    p.add_argument("--pr", type=int, required=True, help="PR number")
    p.add_argument("--head-ref", default="", help="Override the PR's headRefName")
    p.add_argument("--head", default="", help="Override the head sha used as the counter key")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Check the budget and report, without charging or running an agent",
    )
    _add_common(p)


def register_routing_commands(sub: argparse._SubParsersAction) -> None:
    """Register the ``route`` and ``pr`` command trees on the top-level parser."""
    route_p = sub.add_parser(
        "route",
        help="Decide the next action for a PR from its gate verdict history (read-only)",
    )
    route_p.add_argument("--head", default="", help="Current head sha")
    route_p.add_argument("--lane", default="", help="Lane slug for the counter key")
    _add_common(route_p)
    route_p.set_defaults(func=cmd_route)

    pr_p = sub.add_parser("pr", help="Per-PR post-gate actions")
    pr_sub = pr_p.add_subparsers(dest="pr_command", required=True)
    for name, func, help_text in (
        ("rebase", cmd_pr_rebase, "Merge the base in and resolve conflicts, then re-gate"),
        ("repair", cmd_pr_repair, "Make the PR's tests runnable, then re-gate"),
    ):
        action_p = pr_sub.add_parser(name, help=help_text)
        _add_action_args(action_p)
        action_p.set_defaults(func=func)
