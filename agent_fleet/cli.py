#!/usr/bin/env python3
"""CLI for agent_fleet."""

from __future__ import annotations

import argparse
import importlib.resources
import json
import subprocess
import sys
from pathlib import Path

from agent_fleet.backends import make_backend
from agent_fleet.cli_core import normalize_argv
from agent_fleet.cli_env import require_backend_env
from agent_fleet.config import load_fleet_config
from agent_fleet.context import ContextOptions, build_fleet_context
from agent_fleet.dispatcher import FleetDispatcher
from agent_fleet.emit import emit
from agent_fleet.personas import YamlPersonaResolver
from agent_fleet.repo import RepoConfig, find_repo_config
from agent_fleet.runner import run_full_pipeline


def cmd_review(args: argparse.Namespace) -> int:
    from agent_fleet.pr_review.runner import run_pr_review

    ctx, err = build_fleet_context(
        ContextOptions(
            workspace_arg=args.workspace,
            config_arg=args.config,
            require_env=True,
        )
    )
    if err is not None:
        return err
    assert ctx is not None

    result = run_pr_review(
        workspace=ctx.workspace,
        fleet_config=ctx.config,
        base_branch=args.base or "main",
        pr_number=args.pr_number or 0,
    )
    return emit(result, fmt=args.format)


def cmd_scope(args: argparse.Namespace) -> int:
    from agent_fleet.fleet_scope import run_scope

    ctx, err = build_fleet_context(
        ContextOptions(
            workspace_arg=args.workspace,
            config_arg=args.config,
            require_env=True,
        )
    )
    if err is not None:
        return err
    assert ctx is not None

    result = run_scope(
        workspace=ctx.workspace,
        fleet_config=ctx.config,
        github_repo=args.github_repo,
        issue_limit=args.issue_limit,
    )
    return emit(result)


def cmd_scout(args: argparse.Namespace) -> int:
    from agent_fleet.scouts import run_scout

    ctx, err = build_fleet_context(
        ContextOptions(
            workspace_arg=args.workspace,
            config_arg=args.config,
            require_env=True,
        )
    )
    if err is not None:
        return err
    assert ctx is not None

    result = run_scout(
        workspace=ctx.workspace,
        fleet_config=ctx.config,
        github_repo=args.github_repo,
        issue_limit=args.issue_limit,
        product_context=args.product_context or "",
        depth=args.depth,
    )
    return emit(result)


def cmd_run(args: argparse.Namespace) -> int:
    # Build context with require_env=False so dry_run can early-return first.
    ctx, err = build_fleet_context(
        ContextOptions(
            workspace_arg=args.workspace,
            config_arg=args.config,
            persona_arg=args.persona,
            require_env=False,
            personas_dir_from_repo=True,
        )
    )
    if err is not None:
        return err
    assert ctx is not None

    if getattr(args, "dry_run", False):
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "goal": args.goal,
                    "title": args.title,
                    "context": args.context or "",
                    "persona": ctx.persona,
                    "pipeline": args.pipeline,
                    "workspace": str(ctx.workspace),
                    "backend": ctx.config.default_backend,
                    "repo_config": str(ctx.repo.repo_root) if ctx.repo else None,
                },
                indent=2,
                default=str,
            )
        )
        return 0

    # Env guard must come after dry_run early-return, never before it.
    if (code := require_backend_env(ctx.config)) is not None:
        return code

    if args.pipeline == "full":
        resolver = YamlPersonaResolver(ctx.config)
        backend = make_backend(ctx.config)
        result = run_full_pipeline(
            goal=args.goal,
            context=args.context or "",
            title=args.title,
            persona=ctx.persona,
            workspace=ctx.workspace,
            backend=backend,
            persona_resolver=resolver,
        )
        # Full-pipeline result carries .outcome; emit() uses the full-pipeline
        # ok-set which includes completed_noop and review_changes_requested.
        return emit(result.__dict__)

    if args.max_redispatches is not None:
        ctx.config.max_redispatches = args.max_redispatches
    dispatcher = FleetDispatcher(config=ctx.config)
    results = dispatcher.dispatch(
        goal=args.goal,
        context=args.context,
        persona=args.persona,
        workspace=str(ctx.workspace),
        pipeline=args.pipeline,
    )
    # Dispatcher result is a list; emit() uses the dispatcher ok-set (status
    # field) which does NOT include completed_noop or review_changes_requested.
    return emit([r.__dict__ for r in results])


def cmd_personas(args: argparse.Namespace) -> int:
    ctx, err = build_fleet_context(
        ContextOptions(
            workspace_arg=args.workspace,
            config_arg=args.config,
            personas_dir_from_repo=True,
        )
    )
    if err is not None:
        return err
    assert ctx is not None

    resolver = YamlPersonaResolver(ctx.config)
    return emit({"personas": resolver.list_personas(), "pipelines": ctx.config.pipelines})


def cmd_loop(args: argparse.Namespace) -> int:
    from agent_fleet.pr_loop.lifecycle import run_pr_lifecycle
    from agent_fleet.pr_loop.watcher import PrLoopWatcher, run_watcher_once
    from agent_fleet.telemetry import configure_fleet_logging

    configure_fleet_logging()

    workspace = Path(args.workspace or Path.cwd()).resolve()
    if args.once:
        results = run_watcher_once(workspace)
        print(json.dumps(results, indent=2))
        return 0

    ctx, err = build_fleet_context(
        ContextOptions(
            workspace_arg=args.workspace,
            config_arg=args.config,
            require_env=True,
        )
    )
    if err is not None:
        return err
    assert ctx is not None

    repo = ctx.repo
    if repo is None or repo.pr_loop is None or not repo.pr_loop.enabled:
        print("error: pr_loop.enabled not set in .agent-fleet.yaml", file=sys.stderr)
        return 1

    watcher = PrLoopWatcher(repo, repo.pr_loop, fleet_config=ctx.config)
    if args.pr_number:
        branch = args.branch
        base_ref_name = ""
        import subprocess

        view = subprocess.run(
            ["gh", "pr", "view", str(args.pr_number), "--json", "headRefName,baseRefName"],
            capture_output=True,
            text=True,
            check=False,
            cwd=ctx.workspace,
        )
        if view.returncode == 0:
            payload = json.loads(view.stdout)
            if not branch:
                branch = payload.get("headRefName", "")
            base_ref_name = payload.get("baseRefName", "")
        if not base_ref_name:
            print(
                f"warning: could not resolve baseRefName for PR #{args.pr_number}; "
                f"drift will be judged against repo.default_branch",
                file=sys.stderr,
            )
        if not branch:
            print("error: --branch required or gh must resolve PR head", file=sys.stderr)
            return 1

        result = run_pr_lifecycle(
            pr_number=args.pr_number,
            branch=branch,
            repo=repo,
            loop_config=repo.pr_loop,
            fleet_config=ctx.config,
            skip_review_wait=bool(args.skip_review_wait),
            base_ref_name=base_ref_name,
        )
        print(json.dumps({"status": result.status, "detail": result.detail}, indent=2))
        return 0 if result.status in {"merged", "ready", "no_findings", "ci_green"} else 1

    watcher.run_forever()
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.path or Path.cwd()).resolve()
    target.mkdir(parents=True, exist_ok=True)
    dest = target / ".agent-fleet.yaml"
    if dest.exists() and not args.force:
        print(f"already exists: {dest}", file=sys.stderr)
        return 1
    template_text = (
        importlib.resources.files("agent_fleet.templates")
        .joinpath("repo.agent-fleet.yaml")
        .read_text(encoding="utf-8")
    )
    dest.write_text(template_text, encoding="utf-8")
    print(f"created {dest}")
    return 0


def cmd_bridge(args: argparse.Namespace) -> int:
    from agent_fleet.bridge_daemon import (
        start_bridge,
        status_bridge,
        stop_bridge,
    )

    action = args.bridge_action
    if action == "start":
        try:
            state = start_bridge(workspace=args.workspace, timeout_s=args.timeout)
        except Exception as exc:
            print(f"bridge start failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(_redact_state(state), indent=2))
        return 0
    if action == "stop":
        result = stop_bridge()
        print(json.dumps(result, indent=2))
        return 0 if result.get("stopped") or result.get("reason") else 1
    if action == "status":
        result = status_bridge()
        print(json.dumps(result, indent=2))
        return 0 if result.get("running") else 1
    print(f"unknown bridge action: {action}", file=sys.stderr)
    return 2


def _redact_state(state: dict) -> dict:
    redacted = dict(state)
    token = redacted.get("auth_token")
    if isinstance(token, str) and token:
        redacted["auth_token"] = f"<{len(token)} chars>"
    return redacted


def _resolve_repo_from_path(repo_path: Path) -> RepoConfig:
    from agent_fleet.repo import find_repo_config, load_repo_config

    repo_path = repo_path.resolve()
    if repo_path.is_file():
        return load_repo_config(repo_path)
    repo = find_repo_config(repo_path)
    if repo is None:
        raise ValueError(f"No .agent-fleet.yaml found under {repo_path}")
    return repo


def cmd_level_up_status(args: argparse.Namespace) -> int:
    from agent_fleet.level_up.overlay import load_overlay
    from agent_fleet.level_up.paths import repo_key

    try:
        repo = _resolve_repo_from_path(Path(args.repo))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    key = repo_key(name=repo.name, repo_root=repo.repo_root)
    overlay = load_overlay(key, args.persona)
    print(
        json.dumps(
            {
                "repo_key": key,
                "persona": args.persona,
                "generation": overlay.generation,
                "rule_count": len(overlay.rules),
            },
            indent=2,
        )
    )
    return 0


def cmd_level_up_journal(args: argparse.Namespace) -> int:
    from agent_fleet.level_up.journal import tail_journal
    from agent_fleet.level_up.paths import repo_key

    try:
        repo = _resolve_repo_from_path(Path(args.repo))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    key = repo_key(name=repo.name, repo_root=repo.repo_root)
    entries = tail_journal(key, args.persona, tail=args.tail)
    print(json.dumps(entries, indent=2, default=str))
    return 0


def cmd_level_up_train(args: argparse.Namespace) -> int:
    from agent_fleet.level_up.paths import repo_key
    from agent_fleet.level_up.train import train_persona

    try:
        repo = _resolve_repo_from_path(Path(args.repo))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    key = repo_key(name=repo.name, repo_root=repo.repo_root)
    if repo.level_up is not None and not repo.level_up.train:
        print(
            json.dumps(
                {
                    "repo_key": key,
                    "persona": args.persona,
                    "skipped": True,
                    "reason": "level_up.train is false",
                },
                indent=2,
            )
        )
        return 0

    contribute = True
    journal_summaries = True
    if repo.level_up is not None:
        contribute = repo.level_up.contribute_to_fleet
        journal_summaries = repo.level_up.journal_task_summaries

    result = train_persona(
        key,
        args.persona,
        contribute_to_fleet=contribute,
        journal_task_summaries=journal_summaries,
        dry_run=args.dry_run,
    )
    print(
        json.dumps(
            {
                "repo_key": key,
                "persona": args.persona,
                "promoted": result.promoted,
                "queued": result.queued,
                "rejected": result.rejected,
                "dry_run": args.dry_run,
            },
            indent=2,
        )
    )
    return 0


def cmd_level_up_approve(args: argparse.Namespace) -> int:
    from agent_fleet.level_up.paths import repo_key
    from agent_fleet.level_up.train import approve_candidate

    try:
        repo = _resolve_repo_from_path(Path(args.repo))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    key = repo_key(name=repo.name, repo_root=repo.repo_root)
    contribute = True
    if repo.level_up is not None:
        contribute = repo.level_up.contribute_to_fleet

    verdict = approve_candidate(
        key,
        args.persona,
        args.candidate,
        contribute_to_fleet=contribute,
        force=args.force,
    )
    print(
        json.dumps(
            {
                "repo_key": key,
                "persona": args.persona,
                "candidate": args.candidate,
                "verdict": verdict,
            },
            indent=2,
        )
    )
    return 0 if verdict == "approve" else 1


def cmd_level_up_compact(args: argparse.Namespace) -> int:
    from agent_fleet.level_up.compaction import compact_persona
    from agent_fleet.level_up.paths import repo_key

    try:
        repo = _resolve_repo_from_path(Path(args.repo))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    key = repo_key(name=repo.name, repo_root=repo.repo_root)
    retired = compact_persona(key, args.persona)
    print(
        json.dumps(
            {
                "repo_key": key,
                "persona": args.persona,
                "retired": retired,
            },
            indent=2,
        )
    )
    return 0


def cmd_level_up_overlap(args: argparse.Namespace) -> int:
    from agent_fleet.level_up.paths import repo_key
    from agent_fleet.level_up.train import find_overlay_overlap

    try:
        repo = _resolve_repo_from_path(Path(args.repo))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    key = repo_key(name=repo.name, repo_root=repo.repo_root)
    overlaps = find_overlay_overlap(key, args.persona)
    print(
        json.dumps(
            {
                "repo_key": key,
                "persona": args.persona,
                "overlaps": overlaps,
            },
            indent=2,
        )
    )
    return 0


def cmd_learn(args: argparse.Namespace) -> int:
    """Trigger the self-improving flywheel for the global fleet tier.

    When a real backend is available, this will actually dispatch the
    fleet-learner persona against ~/.agent-fleet/ and use real LLM synthesis.
    """
    from agent_fleet.learning import synthesize_fleet_skills

    print("Running fleet self-improvement synthesis...")
    print(f"  personas: {args.personas or 'default (coder, reviewer, pr-analyzer)'}")
    print(f"  min_rows: {args.min_rows}")
    print(f"  dry_run:  {args.dry_run}")
    print()

    ctx, err = build_fleet_context(
        ContextOptions(
            config_arg=args.config,
            require_env=True,
        )
    )
    if err is not None:
        return err
    assert ctx is not None

    resolver = YamlPersonaResolver(ctx.config)
    backend = make_backend(ctx.config)

    result = synthesize_fleet_skills(
        personas=args.personas,
        min_experience_rows=args.min_rows,
        dry_run=args.dry_run,
        # Pass real objects so LLM synthesis can actually run the fleet-learner
        backend=backend,
        resolver=resolver,
        fleet_config=ctx.config,
    )

    print("Synthesis complete:")
    print(f"  personas updated:     {result.personas_updated}")
    print(f"  rules proposed:       {result.new_rules_proposed}")
    print(f"  promoted to _fleet:   {result.promoted_to_fleet}")

    if result.promoted_to_fleet > 0:
        print(
            "\nNew skills are now available in the global _fleet tier "
            "and will be equipped on future dispatches."
        )

    return 0


def cmd_pr_analyze(_args: argparse.Namespace) -> int:
    """Thin adapter: run the GitHub Actions PR analyzer via its env-var protocol.

    All configuration is read from environment variables (GITHUB_TOKEN,
    GITHUB_REPOSITORY, AGENT_FLEET_BACKEND, AGENT_FLEET_WORKSPACE, etc.).
    This is the same protocol as the standalone ``agent-fleet-pr-analyzer``
    console script — the subcommand is a passthrough to the adapter.
    """
    from agent_fleet.pr_review.github_action import main as _pr_analyze_main

    return _pr_analyze_main()


def cmd_dispatch(_args: argparse.Namespace) -> int:
    """Thin adapter: run a single issue-triggered fleet dispatch via env-var protocol.

    All configuration is read from environment variables:
      ISSUE_NUMBER, COMMENT_BODY, PERSONA,
      AGENT_FLEET_WORKSPACE (or AGENT_FLEET_TARGET_CONFIG), AGENT_FLEET_CONFIG.

    The silent-cwd safety check (exit 2 when neither workspace env var is set)
    is preserved inside the adapter — this subcommand does not bypass it.
    """
    from agent_fleet.issue_loop.dispatch import main as _dispatch_main

    # The standalone main() uses raise SystemExit(...).  Wrap so we return
    # the exit code rather than propagating the exception.
    try:
        _dispatch_main()
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else (1 if code else 0)
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    """Thin adapter: delegate to the schedule/cli adapter with the remaining argv.

    Passes --workspace and --config through, then appends the schedule
    subcommand (list | tick | run) and any extra args.
    """
    from agent_fleet.schedule.cli import main as _schedule_main

    # Reconstruct argv for the schedule adapter.
    schedule_argv: list[str] = []
    if getattr(args, "workspace", None):
        schedule_argv += ["--workspace", args.workspace]
    if getattr(args, "config", None):
        schedule_argv += ["--config", args.config]
    schedule_argv.append(args.schedule_command)
    if args.schedule_command == "run" and getattr(args, "schedule_id", None):
        schedule_argv += ["--id", args.schedule_id]
    return _schedule_main(schedule_argv)


def cmd_doctor(args: argparse.Namespace) -> int:
    import yaml

    from agent_fleet.doctor import doctor_exit_code, render_doctor, run_doctor_checks

    backend = "cursor"
    try:
        config = load_fleet_config(args.config) if args.config else load_fleet_config()
        backend = config.default_backend
    except OSError, ValueError, yaml.YAMLError:
        pass
    workspace = Path(args.workspace).resolve() if args.workspace else Path.cwd()
    repo_present = find_repo_config(workspace) is not None
    checks = run_doctor_checks(backend=backend, repo_present=repo_present)
    if args.json:
        print(json.dumps([c.to_dict() for c in checks], indent=2))
    else:
        print(render_doctor(checks))
    return doctor_exit_code(checks)


def cmd_runs(args: argparse.Namespace) -> int:
    from agent_fleet.observability.run_store import read_run_index, render_runs_table

    rows = read_run_index()
    if args.limit and args.limit > 0:
        rows = rows[: args.limit]
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
    else:
        print(render_runs_table(rows))
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    import time
    from dataclasses import asdict

    from agent_fleet.observability.run_store import (
        fold_run_events,
        load_fleet_events,
        render_run_state,
        resolve_run_path,
        run_is_terminal,
        run_log_total_tokens,
    )

    path = resolve_run_path(args.run)
    if path is None:
        print(f"error: no run matching {args.run!r} under the runs dir", file=sys.stderr)
        return 1

    if args.json:
        rows = load_fleet_events(path)
        payload = asdict(fold_run_events(rows))
        payload["tokens"] = run_log_total_tokens(rows)
        print(json.dumps(payload, indent=2, default=str))
        return 0

    def snapshot() -> tuple[str, bool]:
        rows = load_fleet_events(path)
        state = fold_run_events(rows)
        return render_run_state(state, tokens=run_log_total_tokens(rows)), run_is_terminal(state)

    text, terminal = snapshot()
    if args.once or terminal:
        print(text)
        return 0

    is_tty = sys.stdout.isatty()
    last_size = -1
    stable_ticks = 0
    try:
        while True:
            text, terminal = snapshot()
            if is_tty:
                print("\033[2J\033[H", end="")
            print(text, flush=True)
            size = path.stat().st_size if path.exists() else 0
            if terminal:
                stable_ticks = stable_ticks + 1 if size == last_size else 0
                if stable_ticks >= 1:
                    break
            last_size = size
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 130
    return 0


def cmd_summon(args: argparse.Namespace) -> int:
    """Idempotent first-run setup: conditional init + doctor + ready banner.

    Running ``fleet summon`` (or bare ``fleet``) is safe to call multiple
    times.  It:
      1. Creates .agent-fleet.yaml in the current directory if absent.
      2. Runs doctor checks and prints them.
      3. Prints a ready banner so the user knows the fleet is operational.
    """
    workspace = Path(getattr(args, "workspace", None) or Path.cwd()).resolve()

    # Step 1: conditional init — only create the config when not already present.
    dest = workspace / ".agent-fleet.yaml"
    if not dest.exists():
        init_args = argparse.Namespace(path=str(workspace), force=False)
        rc = cmd_init(init_args)
        if rc != 0:
            return rc

    # Step 2: run doctor checks (inline, not via build_fleet_context).
    doctor_args = argparse.Namespace(
        workspace=str(workspace),
        config=getattr(args, "config", None),
        json=False,
    )
    doctor_rc = cmd_doctor(doctor_args)

    # Step 3: ready banner.
    print("\nFleet is ready. Run 'fleet run <goal>' to dispatch a task.")

    # Return doctor exit code so env problems surface, but don't block the banner.
    return doctor_rc


def cmd_self_update(_args: argparse.Namespace) -> int:
    """Upgrade agent-fleet itself via ``uv tool upgrade agent-fleet``."""
    try:
        result = subprocess.run(["uv", "tool", "upgrade", "agent-fleet"])
    except FileNotFoundError:
        print(
            "error: uv not found. Install uv (https://docs.astral.sh/uv/) and try again.",
            file=sys.stderr,
        )
        return 1
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    from agent_fleet import __version__

    parser = argparse.ArgumentParser(
        prog="agent-fleet",
        description="Agentic coding fleet CLI",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"fleet {__version__}",
    )
    parser.add_argument(
        "--config",
        help="Path to fleet.yaml (default: ~/.agent-fleet/fleet.yaml)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run a coding task")
    run_p.add_argument("goal", help="Task goal")
    run_p.add_argument("--context", default="", help="Extra context")
    run_p.add_argument("--title", help="Short title for full pipeline")
    run_p.add_argument("--persona", help="Persona id (default: repo or fleet config)")
    run_p.add_argument("--workspace", help="Repo path")
    run_p.add_argument(
        "--pipeline",
        default="simple",
        help="simple | code_review | pr_review | full",
    )
    run_p.add_argument(
        "--max-redispatches",
        type=int,
        default=None,
        help="Override fleet config max_redispatches for this run.",
    )
    run_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve and print the run plan (persona, pipeline, workspace) without dispatching.",
    )
    run_p.set_defaults(func=cmd_run)

    review_p = sub.add_parser("review", help="Run two-pass PR analyzer on workspace diff")
    review_p.add_argument("--workspace", help="Repo path")
    review_p.add_argument("--base", default="main", help="Base branch for merge-base diff")
    review_p.add_argument("--pr-number", type=int, default=0, help="PR number for logs")
    review_p.add_argument(
        "--format",
        choices=("json", "comment"),
        default="json",
        help="Output JSON result or GitHub comment markdown",
    )
    review_p.set_defaults(func=cmd_review)

    scope_p = sub.add_parser(
        "scope",
        help="Rank fleet-dispatchable tasks using thermo-nuclear quality review",
    )
    scope_p.add_argument("--workspace", help="Repo path")
    scope_p.add_argument("--github-repo", help="owner/repo override for gh issues")
    scope_p.add_argument("--issue-limit", type=int, default=20)
    scope_p.set_defaults(func=cmd_scope)

    scout_p = sub.add_parser(
        "scout",
        help="Fleet Scouts — product + technical intake (read-only)",
    )
    scout_p.add_argument("--workspace", help="Repo path")
    scout_p.add_argument("--github-repo", help="owner/repo override for gh issues")
    scout_p.add_argument("--issue-limit", type=int, default=20)
    scout_p.add_argument("--product-context", help="Extra product/business context")
    scout_p.add_argument(
        "--depth",
        choices=("light", "deep"),
        default="light",
        help="Scout depth (default: light)",
    )
    scout_p.set_defaults(func=cmd_scout)

    personas_p = sub.add_parser("personas", help="List personas")
    personas_p.add_argument("--workspace", help="Repo path (for repo-local personas)")
    personas_p.set_defaults(func=cmd_personas)

    doctor_p = sub.add_parser("doctor", help="Preflight environment checks with actionable fixes")
    doctor_p.add_argument("--workspace", help="Repo path (checks for .agent-fleet.yaml)")
    doctor_p.add_argument("--json", action="store_true", help="Emit checks as JSON")
    doctor_p.set_defaults(func=cmd_doctor)

    runs_p = sub.add_parser("runs", help="List recorded fleet runs (newest first)")
    runs_p.add_argument("--limit", type=int, default=20, help="Max rows to show (default 20)")
    runs_p.add_argument("--json", action="store_true", help="Emit rows as JSON")
    runs_p.set_defaults(func=cmd_runs)

    watch_p = sub.add_parser(
        "watch", help="Live phase/agent tree for a run (by id, prefix, or 'latest')"
    )
    watch_p.add_argument("run", nargs="?", default="latest", help="Run id, id prefix, or 'latest'")
    watch_p.add_argument("--once", action="store_true", help="Render once and exit")
    watch_p.add_argument(
        "--json", action="store_true", help="Emit the folded run state as JSON and exit"
    )
    watch_p.add_argument("--interval", type=float, default=1.0, help="Poll seconds (default 1.0)")
    watch_p.set_defaults(func=cmd_watch)

    loop_p = sub.add_parser("loop", help="Run PR review-fix-merge watcher")
    loop_p.add_argument("--workspace", help="Repo path")
    loop_p.add_argument("--once", action="store_true", help="Poll open fleet PRs once")
    loop_p.add_argument("--pr-number", type=int, help="Run lifecycle for one PR")
    loop_p.add_argument("--branch", help="Head branch (required with --pr-number)")
    loop_p.add_argument(
        "--skip-review-wait",
        action="store_true",
        help="Do not wait for analyzer comment when running a single PR",
    )
    loop_p.set_defaults(func=cmd_loop)

    init_p = sub.add_parser("init", help="Create .agent-fleet.yaml in a repo")
    init_p.add_argument("path", nargs="?", help="Repo path")
    init_p.add_argument("--force", action="store_true")
    init_p.set_defaults(func=cmd_init)

    bridge_p = sub.add_parser(
        "bridge",
        help="Manage a shared cursor-sdk bridge daemon (enables concurrent agent-fleet runs)",
    )
    bridge_sub = bridge_p.add_subparsers(dest="bridge_action", required=True)
    bridge_start_p = bridge_sub.add_parser(
        "start", help="Start a shared bridge daemon (idempotent)"
    )
    bridge_start_p.add_argument(
        "--workspace",
        default=None,
        help="Workspace path passed to cursor-sdk-bridge (defaults to ~/.agent-fleet)",
    )
    bridge_start_p.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for the bridge discovery line",
    )
    bridge_start_p.set_defaults(func=cmd_bridge)
    bridge_stop_p = bridge_sub.add_parser("stop", help="Stop the shared bridge daemon")
    bridge_stop_p.set_defaults(func=cmd_bridge)
    bridge_status_p = bridge_sub.add_parser("status", help="Show shared bridge daemon status")
    bridge_status_p.set_defaults(func=cmd_bridge)

    level_up_p = sub.add_parser("level-up", help="Persona level-up status and journal")
    level_up_sub = level_up_p.add_subparsers(dest="level_up_command", required=True)

    level_up_status_p = level_up_sub.add_parser(
        "status",
        help="Show overlay generation and rule count for a persona",
    )
    level_up_status_p.add_argument("--repo", required=True, help="Repo path or .agent-fleet.yaml")
    level_up_status_p.add_argument("--persona", required=True, help="Persona name")
    level_up_status_p.set_defaults(func=cmd_level_up_status)

    level_up_journal_p = level_up_sub.add_parser(
        "journal",
        help="Tail persona level-up journal events",
    )
    level_up_journal_p.add_argument("--repo", required=True, help="Repo path or .agent-fleet.yaml")
    level_up_journal_p.add_argument("--persona", required=True, help="Persona name")
    level_up_journal_p.add_argument("--tail", type=int, default=20, help="Number of events")
    level_up_journal_p.set_defaults(func=cmd_level_up_journal)

    level_up_train_p = level_up_sub.add_parser(
        "train",
        help="Mine experience and promote gated overlay rules",
    )
    level_up_train_p.add_argument("--repo", required=True, help="Repo path or .agent-fleet.yaml")
    level_up_train_p.add_argument("--persona", required=True, help="Persona name")
    level_up_train_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Propose promotions without writing overlay",
    )
    level_up_train_p.set_defaults(func=cmd_level_up_train)

    level_up_approve_p = level_up_sub.add_parser(
        "approve",
        help="Tech-lead approve a queued skill candidate",
    )
    level_up_approve_p.add_argument("--repo", required=True, help="Repo path or .agent-fleet.yaml")
    level_up_approve_p.add_argument("--persona", required=True, help="Persona name")
    level_up_approve_p.add_argument("--candidate", required=True, help="Candidate id")
    level_up_approve_p.add_argument(
        "--force",
        action="store_true",
        help="Approve without LLM tech-lead review (heuristic only)",
    )
    level_up_approve_p.set_defaults(func=cmd_level_up_approve)

    level_up_compact_p = level_up_sub.add_parser(
        "compact",
        help="Retire idle overlay rules (7-day default)",
    )
    level_up_compact_p.add_argument("--repo", required=True, help="Repo path or .agent-fleet.yaml")
    level_up_compact_p.add_argument("--persona", required=True, help="Persona name")
    level_up_compact_p.set_defaults(func=cmd_level_up_compact)

    level_up_overlap_p = level_up_sub.add_parser(
        "overlap",
        help="List rule ids present in repo and fleet overlays",
    )
    level_up_overlap_p.add_argument("--repo", required=True, help="Repo path or .agent-fleet.yaml")
    level_up_overlap_p.add_argument("--persona", required=True, help="Persona name")
    level_up_overlap_p.set_defaults(func=cmd_level_up_overlap)

    # --- Self-improving flywheel (cross-repo skill synthesis) ---
    learn_p = sub.add_parser(
        "learn",
        help=(
            "Run the fleet self-improvement flywheel "
            "(synthesizes skills across repos into the global _fleet tier)"
        ),
    )
    learn_p.add_argument(
        "--personas",
        nargs="*",
        default=None,
        help="Personas to synthesize for (default: coder reviewer pr-analyzer)",
    )
    learn_p.add_argument("--dry-run", action="store_true", help="Propose but do not promote")
    learn_p.add_argument(
        "--min-rows",
        type=int,
        default=20,
        help="Min total experience rows before synthesizing",
    )
    learn_p.set_defaults(func=cmd_learn)

    # --- P3: folded entry points ---

    pr_analyze_p = sub.add_parser(
        "pr-analyze",
        help=(
            "Run the GitHub Actions PR analyzer (env-var protocol: "
            "GITHUB_TOKEN, GITHUB_REPOSITORY, AGENT_FLEET_BACKEND, …)"
        ),
    )
    pr_analyze_p.set_defaults(func=cmd_pr_analyze)

    dispatch_p = sub.add_parser(
        "dispatch",
        help=(
            "Run a single issue-triggered fleet dispatch (env-var protocol: "
            "ISSUE_NUMBER, PERSONA, AGENT_FLEET_WORKSPACE, …)"
        ),
    )
    dispatch_p.set_defaults(func=cmd_dispatch)

    schedule_p = sub.add_parser(
        "schedule",
        help="Cron-based scheduled fleet dispatch (list | tick | run subcommands)",
    )
    schedule_p.add_argument("--workspace", help="Repo path (default: cwd)")
    schedule_sub = schedule_p.add_subparsers(dest="schedule_command", required=True)

    schedule_list_p = schedule_sub.add_parser(
        "list", help="List configured schedules and next due times"
    )
    schedule_list_p.add_argument("--workspace", help="Repo path (default: cwd)")
    schedule_list_p.set_defaults(func=cmd_schedule)

    schedule_tick_p = schedule_sub.add_parser("tick", help="Evaluate schedules once")
    schedule_tick_p.add_argument("--workspace", help="Repo path (default: cwd)")
    schedule_tick_p.set_defaults(func=cmd_schedule)

    schedule_run_p = schedule_sub.add_parser("run", help="Manually fire one schedule by id")
    schedule_run_p.add_argument("--workspace", help="Repo path (default: cwd)")
    schedule_run_p.add_argument("--id", dest="schedule_id", required=True, help="Schedule job id")
    schedule_run_p.set_defaults(func=cmd_schedule)

    from agent_fleet.workstreams.cli import register_workstream_commands

    register_workstream_commands(sub)

    from agent_fleet.orchestration.dag.cli import register_dag_commands

    register_dag_commands(sub)

    summon_p = sub.add_parser(
        "summon",
        help="First-run setup: init config (if absent) + doctor + ready banner (idempotent)",
    )
    summon_p.add_argument("--workspace", help="Repo path (default: cwd)")
    summon_p.set_defaults(func=cmd_summon)

    self_p = sub.add_parser("self", help="Maintenance commands for agent-fleet itself")
    self_sub = self_p.add_subparsers(dest="self_command", required=True)
    self_update_p = self_sub.add_parser(
        "update",
        help="Upgrade agent-fleet to the latest published version via uv",
    )
    self_update_p.set_defaults(func=cmd_self_update)

    # Normalize argv before parsing so bare invocation and plain-goal shortcuts work.
    # known_subcommands is derived from sub.choices at this point (all subparsers registered).
    if argv is None:
        import sys

        raw = sys.argv[1:]
    else:
        raw = list(argv)

    normalized = normalize_argv(raw, set(sub.choices), Path.cwd())

    args = parser.parse_args(normalized)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
