#!/usr/bin/env python3
"""CLI for agent_fleet."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agent_fleet.backends import make_backend
from agent_fleet.cli_env import require_backend_env
from agent_fleet.config import load_fleet_config
from agent_fleet.dispatcher import FleetDispatcher
from agent_fleet.personas import YamlPersonaResolver
from agent_fleet.repo import find_repo_config
from agent_fleet.runner import run_full_pipeline


def cmd_review(args: argparse.Namespace) -> int:
    from agent_fleet.pr_review.runner import run_pr_review

    workspace = Path(args.workspace or Path.cwd()).resolve()
    config = load_fleet_config(args.config) if args.config else load_fleet_config()
    if (code := require_backend_env(config)) is not None:
        return code

    result = run_pr_review(
        workspace=workspace,
        fleet_config=config,
        base_branch=args.base or "main",
        pr_number=args.pr_number or 0,
    )
    if args.format == "comment":
        print(result["comment_markdown"])
    else:
        print(json.dumps(result, indent=2, default=str))
    verdict = str(result["verdict"])
    return 0 if verdict == "approve" else 1


def cmd_scope(args: argparse.Namespace) -> int:
    from agent_fleet.fleet_scope import run_scope

    workspace = Path(args.workspace or Path.cwd()).resolve()
    config = load_fleet_config(args.config) if args.config else load_fleet_config()
    if (code := require_backend_env(config)) is not None:
        return code

    result = run_scope(
        workspace=workspace,
        fleet_config=config,
        github_repo=args.github_repo,
        issue_limit=args.issue_limit,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0 if "error" not in result else 1


def cmd_scout(args: argparse.Namespace) -> int:
    from agent_fleet.scouts import run_scout

    workspace = Path(args.workspace or Path.cwd()).resolve()
    config = load_fleet_config(args.config) if args.config else load_fleet_config()
    if (code := require_backend_env(config)) is not None:
        return code

    result = run_scout(
        workspace=workspace,
        fleet_config=config,
        github_repo=args.github_repo,
        issue_limit=args.issue_limit,
        product_context=args.product_context or "",
        depth=args.depth,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0 if "error" not in result else 1


def cmd_run(args: argparse.Namespace) -> int:
    config = load_fleet_config(args.config) if args.config else load_fleet_config()
    if (code := require_backend_env(config)) is not None:
        return code

    workspace = Path(args.workspace or Path.cwd()).resolve()
    repo = find_repo_config(workspace)

    if args.pipeline == "full":
        if repo and repo.personas_dir:
            config.personas_dir = repo.personas_dir
        resolver = YamlPersonaResolver(config)
        backend = make_backend(config)
        result = run_full_pipeline(
            goal=args.goal,
            context=args.context or "",
            title=args.title,
            persona=args.persona or (repo.default_persona if repo else config.default_persona),
            workspace=workspace,
            backend=backend,
            persona_resolver=resolver,
        )
        print(json.dumps(result.__dict__, indent=2, default=str))
        ok = {"completed", "completed_noop", "review_changes_requested", "decompose_partial"}
        return 0 if result.outcome in ok else 1

    if args.max_redispatches is not None:
        config.max_redispatches = args.max_redispatches
    dispatcher = FleetDispatcher(config=config)
    results = dispatcher.dispatch(
        goal=args.goal,
        context=args.context,
        persona=args.persona,
        workspace=str(workspace),
        pipeline=args.pipeline,
    )
    print(json.dumps([r.__dict__ for r in results], indent=2, default=str))
    ok = {"completed", "merged", "decompose_partial"}
    return 0 if results and results[0].status in ok else 1


def cmd_personas(args: argparse.Namespace) -> int:
    config = load_fleet_config(args.config)
    workspace = Path(args.workspace or Path.cwd()).resolve()
    repo = find_repo_config(workspace)
    if repo and repo.personas_dir:
        config.personas_dir = repo.personas_dir
    resolver = YamlPersonaResolver(config)
    print(
        json.dumps(
            {"personas": resolver.list_personas(), "pipelines": config.pipelines},
            indent=2,
        )
    )
    return 0


def cmd_loop(args: argparse.Namespace) -> int:
    from agent_fleet.logging_config import configure_fleet_logging
    from agent_fleet.pr_loop.lifecycle import run_pr_lifecycle
    from agent_fleet.pr_loop.watcher import PrLoopWatcher, run_watcher_once

    configure_fleet_logging()

    workspace = Path(args.workspace or Path.cwd()).resolve()
    if args.once:
        results = run_watcher_once(workspace)
        print(json.dumps(results, indent=2))
        return 0

    repo = find_repo_config(workspace)
    if repo is None or repo.pr_loop is None or not repo.pr_loop.enabled:
        print("error: pr_loop.enabled not set in .agent-fleet.yaml", file=sys.stderr)
        return 1

    config = load_fleet_config(args.config) if args.config else load_fleet_config()
    if (code := require_backend_env(config)) is not None:
        return code

    watcher = PrLoopWatcher(repo, repo.pr_loop, fleet_config=config)
    if args.pr_number:
        branch = args.branch
        if not branch:
            import subprocess

            result = subprocess.run(
                ["gh", "pr", "view", str(args.pr_number), "--json", "headRefName"],
                capture_output=True,
                text=True,
                check=False,
                cwd=workspace,
            )
            if result.returncode != 0:
                print("error: --branch required or gh must resolve PR head", file=sys.stderr)
                return 1
            branch = json.loads(result.stdout).get("headRefName", "")

        result = run_pr_lifecycle(
            pr_number=args.pr_number,
            branch=branch,
            repo=repo,
            loop_config=repo.pr_loop,
            fleet_config=config,
            skip_review_wait=bool(args.skip_review_wait),
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
    example = Path(__file__).resolve().parent.parent / "examples" / "repo.agent-fleet.yaml"
    dest.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"created {dest}")
    return 0


def _resolve_repo_from_path(repo_path: Path):
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-fleet", description="Agentic coding fleet CLI")
    parser.add_argument(
        "--config",
        help="Path to fleet.yaml (default: ~/.hermes/coding_fleet/fleet.yaml)",
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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
