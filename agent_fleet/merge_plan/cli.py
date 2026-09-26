"""``agent-fleet merge`` — execute the batches merge-plan produces.

Registered via :func:`register_merge_commands`, mirroring
``fleet_ops.cli.register_lane_commands``, so ``cli.py`` only gains a two-line
import and call and the argparse surface stays in one place.

The command tree is deliberately separate from the flat ``merge-plan``:
planning is read-only and safe to run at any time, while ``merge run`` merges,
deploys, and touches production.  Keeping them apart means a habit of running
``merge-plan`` never becomes an accidental deploy.
"""

from __future__ import annotations

import contextlib
import json
import signal
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable

    from agent_fleet.merge_plan.execute import EventSink, TickResult
    from agent_fleet.merge_plan.types import ExecutorSpec


def _spec(args: argparse.Namespace) -> ExecutorSpec:
    """The executor settings, with a malformed block reported not swallowed."""
    from agent_fleet.merge_plan.config import load_executor_spec

    path = getattr(args, "config", None)
    return load_executor_spec(Path(path) if path else None)


def _spec_or_error(args: argparse.Namespace) -> ExecutorSpec | int:
    """The executor settings, or the exit code to report a malformed block.

    Every ``fleet merge`` subcommand reads the same strictly-validated block, so
    the conversion from ``ValueError`` to ``error: ...`` + exit 2 belongs here
    rather than in each command: a subcommand that forgets it prints a Python
    traceback at an operator who only mistyped a key.
    """
    try:
        return _spec(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _make_run_log(run_id: str) -> EventSink | None:
    """A RunLog for this tick, or ``None`` when observability is unavailable.

    Events must never be the reason a merge does not happen, so a failure to
    build the sink degrades to emitting nothing rather than raising.
    """
    try:
        from agent_fleet.observability.log import RunLog

        return RunLog.create(run_id=run_id, include_memory_ring=False)
    except Exception:
        return None


def cmd_merge_run(args: argparse.Namespace) -> int:
    """Run one merge tick, or loop with --daemon."""
    from agent_fleet.merge_plan.execute import run_tick

    config_path = getattr(args, "config", None)
    spec = _spec_or_error(args)
    if isinstance(spec, int):
        return spec

    repo_specs = list(args.repo_path or [])
    if not repo_specs and not _has_configured_repos(config_path):
        print(
            "error: no repos selected. Pass --repo-path PATH (repeatable) or "
            "add merge_plan.repos[] to fleet.yaml.",
            file=sys.stderr,
        )
        return 1

    kwargs: dict[str, Any] = {
        "repo_paths": repo_specs,
        "fleet_config_path": Path(config_path) if config_path else None,
        "spec": spec,
        "operator": None if args.operator in (None, "all") else args.operator,
        "status_dir": Path(args.status_dir).expanduser() if args.status_dir else None,
        "max_batch_size": args.max_batch_size,
        "check_merges": not args.no_merge_check,
        "dry_run": args.dry_run,
    }

    if not args.daemon:
        run_id = f"merge-run-{int(time.time())}"
        result = run_tick(**kwargs, run_log=_make_run_log(run_id), run_id=run_id)
        return _report([result], args.json)

    stop = _stop_flag()
    interval = max(1.0, float(args.daemon))
    results: list[Any] = []
    while not stop():
        run_id = f"merge-run-{int(time.time())}"
        result = run_tick(**kwargs, run_log=_make_run_log(run_id), run_id=run_id)
        # One JSON document per tick, so a daemon stays machine-readable.
        print(
            json.dumps(result.to_dict(), indent=2, default=str)
            if args.json
            else result.render_text(),
            flush=True,
        )
        results.append(result)
        time.sleep(interval)
    return 1 if any(o.status == "failed" for r in results for o in r.outcomes) else 0


def _has_configured_repos(config_path: str | None) -> bool:
    from agent_fleet.merge_plan.config import load_merge_plan_config

    return bool(load_merge_plan_config(Path(config_path) if config_path else None))


def _stop_flag() -> Callable[[], bool]:
    """A closure that becomes true once SIGINT/SIGTERM asks us to stop.

    A daemon left running after the terminal closes is a daemon that keeps
    merging, so the signal sets a flag the loop checks and the loop leaves the
    deploy lock on its own ``finally`` rather than being torn down mid-batch.
    """
    flag = {"stop": False}

    def _handle(signum: int, frame: object) -> None:
        # The handler's contract fixes the signature; the body only sets a flag.
        del signum, frame
        flag["stop"] = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        # Not on the main thread, or not a signal-capable platform: the daemon
        # then runs until it is stopped some other way.
        with contextlib.suppress(OSError, ValueError):
            signal.signal(sig, _handle)
    return lambda: flag["stop"]


def _report(results: list[TickResult], as_json: bool) -> int:
    """Print the tick(s) and derive an exit code.

    0 when everything that ran succeeded, 1 when a batch failed.  A held or
    locked batch is not a failure: it is the scheduler working, and a cron job
    must not page an operator for it.
    """
    if as_json:
        payload = results[0].to_dict() if len(results) == 1 else [r.to_dict() for r in results]
        print(json.dumps(payload, indent=2, default=str))
    else:
        for result in results:
            print(result.render_text())
    failed = any(o.status == "failed" for r in results for o in r.outcomes)
    return 1 if failed else 0


def cmd_merge_holds(args: argparse.Namespace) -> int:
    """Show which cluster holds are active and which have been released."""
    from agent_fleet.merge_plan.execute import load_ledger

    spec = _spec_or_error(args)
    if isinstance(spec, int):
        return spec
    ledger = load_ledger(spec)
    active = ledger.active_holds(spec)
    released = sorted(ledger.released_holds())
    payload = {
        "active": [h.to_dict() for h in active],
        "released": released,
        "configured": [h.name for h in spec.holds],
    }
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
        return 0
    if not active:
        print("no active cluster holds")
    for hold in active:
        match = []
        if hold.lanes:
            match.append(f"lanes {', '.join(hold.lanes)}")
        if hold.deploy_units:
            match.append(f"deploy units {', '.join(hold.deploy_units)}")
        print(f"held: {hold.name}  [{'; '.join(match) or 'matches nothing'}]")
        print(f"  release: fleet merge release {hold.name}")
    if released:
        print(f"released: {', '.join(released)}")
    return 0


def cmd_merge_release(args: argparse.Namespace) -> int:
    """Clear a named cluster hold so its lanes merge again."""
    from agent_fleet.merge_plan.execute import release_hold

    spec = _spec_or_error(args)
    if isinstance(spec, int):
        return spec
    known = {h.name for h in spec.holds}
    if args.hold not in known:
        known_list = ", ".join(sorted(known)) or "(none configured)"
        print(f"error: unknown hold {args.hold!r}; configured holds: {known_list}", file=sys.stderr)
        return 2
    if release_hold(spec, args.hold):
        print(f"released {args.hold}")
        return 0
    print(f"{args.hold} was already released")
    return 0


def register_merge_commands(sub: argparse._SubParsersAction) -> None:
    """Register the ``merge`` command tree on the top-level parser."""
    merge_p = sub.add_parser(
        "merge",
        help="Execute merge-plan batches: merge, deploy, and verify approved PRs",
    )
    merge_sub = merge_p.add_subparsers(dest="merge_command", required=True)

    run_p = merge_sub.add_parser(
        "run",
        help="Run one merge tick (or loop with --daemon)",
    )
    run_p.add_argument(
        "--repo-path",
        action="append",
        help="Checkout to merge for (repeatable). Overrides fleet.yaml merge_plan.repos[]",
    )
    run_p.add_argument(
        "--config",
        default=None,
        help="Path to fleet.yaml (default: ~/.agent-fleet/fleet.yaml)",
    )
    run_p.add_argument(
        "--operator",
        default=None,
        help="Only read lanes for this operator (default: all operators)",
    )
    run_p.add_argument(
        "--status-dir",
        help="Directory of gate status files containing PREMERGE-APPROVED <sha> lines",
    )
    run_p.add_argument(
        "--max-batch-size",
        type=int,
        default=5,
        help="Cap on PRs per batch (default 5)",
    )
    run_p.add_argument(
        "--no-merge-check",
        action="store_true",
        help="Skip the git merge-compatibility check when planning",
    )
    run_p.add_argument(
        "--daemon",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Loop, running a tick every SECONDS instead of exiting after one",
    )
    run_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would run without executing commands or taking locks",
    )
    run_p.add_argument("--json", action="store_true", help="Emit the result as JSON")
    run_p.set_defaults(func=cmd_merge_run)

    holds_p = merge_sub.add_parser(
        "holds",
        help="Show active and released cluster holds",
    )
    holds_p.add_argument(
        "--config",
        default=None,
        help="Path to fleet.yaml (default: ~/.agent-fleet/fleet.yaml)",
    )
    holds_p.add_argument("--json", action="store_true", help="Emit the result as JSON")
    holds_p.set_defaults(func=cmd_merge_holds)

    release_p = merge_sub.add_parser(
        "release",
        help="Release a named cluster hold",
    )
    release_p.add_argument("hold", help="Hold name, as configured in merge_plan.executor.holds")
    release_p.add_argument(
        "--config",
        default=None,
        help="Path to fleet.yaml (default: ~/.agent-fleet/fleet.yaml)",
    )
    release_p.set_defaults(func=cmd_merge_release)
