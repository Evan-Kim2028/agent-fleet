"""``fleet serve`` — start, inspect and stop the supervisor.

Four subcommands, deliberately few. ``serve`` runs it, ``serve status`` shows
it, ``serve stop`` ends it, and ``serve watchdog --dry-run`` shows what the
watchdog *would* kill without killing it.

``--config`` is deliberately **not** re-registered here. The top-level parser
already defines it, and argparse resolves a subparser's default onto the same
``dest`` — so a second ``--config`` on ``serve`` would silently discard the
value passed as ``fleet --config X serve`` and fall back to the global
``fleet.yaml``. A supervisor quietly running on thresholds the operator did not
choose is worse than an error, so the top-level flag is read as-is and a
serve-specific override is spelled ``--serve-config``.

``serve watchdog --dry-run`` exists because this process kills things. A
self-healing supervisor on a machine shared with other agents should be able to
show its work before it acts, and that is cheap to provide and impossible to
add later once somebody trusts it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_fleet.serve.capacity import read_capacity
from agent_fleet.serve.config import ServeConfigError, load_serve_config
from agent_fleet.serve.escalate import EscalationRouter
from agent_fleet.serve.items import ItemBoard
from agent_fleet.serve.paths import capacity_path, items_path, pid_path, read_json
from agent_fleet.serve.status import render_status, status_snapshot

if TYPE_CHECKING:
    from agent_fleet.serve.clock import Clock
    from agent_fleet.serve.config import ServeConfig
    from agent_fleet.serve.supervisor import Supervisor


def _load(args: argparse.Namespace) -> ServeConfig:
    """Resolve config for a serve subcommand.

    Reads the *top-level* ``--config`` (not a serve-local one) and additionally
    honours ``--serve-config`` as an explicit override. Raises
    :class:`ServeConfigError` with an actionable message rather than falling
    back to defaults when a named file has no serve section.
    """
    serve_config = getattr(args, "serve_config", None)
    override: Path | None = None
    if serve_config:
        override = Path(str(serve_config)).expanduser()
    top_level = getattr(args, "config", None)
    if override is None and top_level:
        candidate = Path(str(top_level)).expanduser()
        # Only treat the global --config as a serve config when it actually
        # configures serve; otherwise it is some other section's file and the
        # global/repo sources still apply.
        if candidate.exists() and _has_serve_section(candidate):
            override = candidate
    repo_root: Path | None = Path.cwd()
    if getattr(args, "repo_root", None) is not None:
        repo_root = Path(str(args.repo_root)).expanduser()
    return load_serve_config(
        operator=str(getattr(args, "operator", "") or ""),
        config_path=override,
        repo_root=repo_root,
    )


def _has_serve_section(path: Path) -> bool:
    import yaml

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError, yaml.YAMLError:
        return False
    if not isinstance(raw, dict):
        return False
    if isinstance(raw.get("serve"), dict):
        return True
    fleet_ops = raw.get("fleet_ops")
    return isinstance(fleet_ops, dict) and isinstance(fleet_ops.get("serve"), dict)


def _clock() -> Clock:
    from agent_fleet.serve.clock import SystemClock

    return SystemClock()


def _supervisor_for(operator: str, config: ServeConfig) -> Supervisor:
    from agent_fleet.serve.supervisor import Supervisor

    return Supervisor(operator, config, clock=_clock())


def cmd_serve_run(args: argparse.Namespace) -> int:
    """Start the supervisor. This is the long-running command."""
    from agent_fleet.serve.serve import ServeLoop

    try:
        config = _load(args)
    except ServeConfigError as exc:
        print(f"fleet serve: {exc}")
        return 2

    operator = args.operator
    if not operator:
        print("fleet serve: --operator is required (serve state is per-operator).")
        return 2

    if args.dry_run_watchdog:
        print(
            "fleet serve: --dry-run-watchdog does not apply to the run loop, which also "
            "supervises components. Use `fleet serve watchdog` (dry run by default) for a "
            "read-only watchdog pass.",
            file=sys.stderr,
        )
        return 2

    loop = ServeLoop(
        operator=operator,
        config=config,
        max_ticks=args.max_ticks,
        dry_run_watchdog=False,
    )
    return loop.run()


def cmd_serve_status(args: argparse.Namespace) -> int:
    """One screen: components, capacity vs pressure, stages, throughput."""
    try:
        config = _load(args)
    except ServeConfigError as exc:
        print(f"fleet serve: {exc}")
        return 2

    operator = args.operator
    if not operator:
        print("fleet serve: --operator is required (serve state is per-operator).")
        return 2

    supervisor = _supervisor_for(operator, config)
    board = ItemBoard(items_path(operator), clock=_clock())
    snapshot = status_snapshot(
        operator,
        supervisor,
        board,
        window_hours=config.throughput_window_hours,
        capacity_file=capacity_path(operator),
    )
    snapshot["pidfile"] = read_json(pid_path(operator))
    if args.json:
        print(json.dumps(snapshot, indent=2, default=str))
    else:
        print(render_status(snapshot))
    return 0


def cmd_serve_stop(args: argparse.Namespace) -> int:
    """Stop a running supervisor, by recorded pid + fingerprint."""
    from agent_fleet.serve.serve import ServeLoop

    operator = args.operator
    if not operator:
        print("fleet serve: --operator is required (serve state is per-operator).")
        return 2
    try:
        config = _load(args)
    except ServeConfigError as exc:
        print(f"fleet serve: {exc}")
        return 2

    loop = ServeLoop(operator=operator, config=config, max_ticks=0)
    if not loop.stop():
        payload = read_json(pid_path(operator))
        if not payload:
            print(f"fleet serve: no supervisor recorded for operator {operator!r}.")
            return 1
        print(
            f"fleet serve: recorded supervisor pid {payload.get('pid')} is not running "
            f"(or its fingerprint changed). Nothing to stop."
        )
        return 1
    print(f"fleet serve: sent SIGTERM to the supervisor for {operator!r}.")
    return 0


def cmd_serve_watchdog(args: argparse.Namespace) -> int:
    """Run the watchdog rules once, read-only by default."""
    from agent_fleet.serve.watchdog import Watchdog

    operator = args.operator
    if not operator:
        print("fleet serve: --operator is required (serve state is per-operator).")
        return 2
    try:
        config = _load(args)
    except ServeConfigError as exc:
        print(f"fleet serve: {exc}")
        return 2

    clock = _clock()
    supervisor = _supervisor_for(operator, config)
    board = ItemBoard(items_path(operator), clock=clock)
    depths = board.depth()
    watchdog = Watchdog(
        operator,
        config,
        supervisor,
        clock=clock,
        dry_run=not args.apply,
    )
    report = watchdog.tick(queued_depth=depths.get("queued", 0) + depths.get("approved", 0))

    payload: dict[str, Any] = {
        "operator": operator,
        "dry_run": watchdog.dry_run,
        "remediations": [r.to_dict() for r in report.remediations],
        "deferred": [r.to_dict() for r in report.deferred],
        "by_rule": report.by_rule(),
    }
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
        return 0

    mode = "WOULD ACT" if watchdog.dry_run else "APPLIED"
    print(f"watchdog {mode} — {len(report.remediations)} remediation(s)")
    for remediation in report.remediations:
        print(f"  [{remediation.rule}] {remediation.subject}: {remediation.action}")
        print(f"      {remediation.reason}")
    if report.deferred:
        print(f"  ({len(report.deferred)} deferred: per-tick budget exhausted)")
    if not report.remediations and not report.deferred:
        print("  nothing to do")
    return 0


def cmd_serve_decisions(args: argparse.Namespace) -> int:
    """Show the human decision queue."""
    operator = args.operator
    if not operator:
        print("fleet serve: --operator is required (serve state is per-operator).")
        return 2
    try:
        config = _load(args)
    except ServeConfigError as exc:
        print(f"fleet serve: {exc}")
        return 2

    clock: Clock = _clock()
    router = EscalationRouter(
        operator,
        clock=clock,
        decisions_file=Path(config.decisions_file) if config.decisions_file else None,
    )
    pending = router.pending()
    if args.json:
        print(json.dumps([d.to_dict() for d in pending], indent=2, default=str))
        return 0
    if not pending:
        print("no decisions pending.")
        return 0
    print(f"{len(pending)} decision(s) pending:")
    for decision in pending:
        print(f"  {decision.item_id}  [{decision.reason_class}]")
        print(f"      {decision.reason}")
    return 0


def cmd_serve_capacity(args: argparse.Namespace) -> int:
    """Print the current capacity file (what dispatch/gate/admission read)."""
    operator = args.operator
    if not operator:
        print("fleet serve: --operator is required (serve state is per-operator).")
        return 2
    document = read_capacity(capacity_path(operator))
    if document is None:
        print(f"no capacity file for {operator!r}; serve has not published one yet.")
        return 1
    print(json.dumps(document, indent=2, default=str))
    return 0


def register_serve_commands(sub: argparse._SubParsersAction) -> None:
    """Register ``serve`` and its subcommands on the top-level parser."""
    serve_p = sub.add_parser(
        "serve",
        help="Supervise the fleet end to end (dispatch, gate, fix, merge) with no babysitter",
    )
    serve_sub = serve_p.add_subparsers(dest="serve_command")

    def add_common(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--operator", default="", help="Operator name (serve state is per-operator)"
        )
        parser.add_argument(
            "--serve-config",
            dest="serve_config",
            default=None,
            help="Explicit serve config file (must contain a `serve:` section)",
        )
        parser.add_argument("--repo-root", default=None, help=argparse.SUPPRESS)

    run_p = serve_sub.add_parser("run", help="Run the supervisor in the foreground (long-running)")
    add_common(run_p)
    run_p.add_argument(
        "--max-ticks",
        type=int,
        default=None,
        help="Stop after N ticks (testing/one-shot); default: run forever",
    )
    run_p.add_argument(
        "--dry-run-watchdog",
        action="store_true",
        help="Refused: use `fleet serve watchdog --dry-run` for a read-only watchdog pass",
    )
    run_p.set_defaults(func=cmd_serve_run)

    status_p = serve_sub.add_parser("status", help="One screen: components, capacity, stages")
    add_common(status_p)
    status_p.add_argument("--json", action="store_true", help="Emit the status snapshot as JSON")
    status_p.set_defaults(func=cmd_serve_status)

    stop_p = serve_sub.add_parser("stop", help="Stop the running supervisor (by recorded pid)")
    add_common(stop_p)
    stop_p.set_defaults(func=cmd_serve_stop)

    wd_p = serve_sub.add_parser(
        "watchdog", help="Run the watchdog rules once (read-only by default)"
    )
    add_common(wd_p)
    wd_p.add_argument(
        "--apply",
        action="store_true",
        help="Actually perform remediations (default: dry run, report only)",
    )
    wd_p.add_argument("--json", action="store_true", help="Emit the watchdog report as JSON")
    wd_p.set_defaults(func=cmd_serve_watchdog)

    dec_p = serve_sub.add_parser("decisions", help="Show the human decision queue")
    add_common(dec_p)
    dec_p.add_argument("--json", action="store_true", help="Emit decisions as JSON")
    dec_p.set_defaults(func=cmd_serve_decisions)

    cap_p = serve_sub.add_parser("capacity", help="Print the current capacity file")
    add_common(cap_p)
    cap_p.set_defaults(func=cmd_serve_capacity)

    # `fleet serve` with no subcommand defaults to `run`, matching the operator's
    # mental model of "fleet serve" meaning "serve".
    serve_p.set_defaults(
        func=cmd_serve_run, serve_command="run", max_ticks=None, dry_run_watchdog=False
    )


__all__ = [
    "cmd_serve_capacity",
    "cmd_serve_decisions",
    "cmd_serve_run",
    "cmd_serve_status",
    "cmd_serve_stop",
    "cmd_serve_watchdog",
    "register_serve_commands",
]
