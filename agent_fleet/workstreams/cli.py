"""CLI handlers for agent-fleet workstream subcommands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from agent_fleet.repo import REPO_CONFIG_NAMES, find_repo_config
from agent_fleet.workstreams.config import WorkstreamsConfig, load_workstreams_config
from agent_fleet.workstreams.dispatch import (
    build_workstream_tasks,
    run_workstreams,
    workstreams_status,
)
from agent_fleet.workstreams.harvest import harvest_worktree, plan_harvest


def load_repo_workstreams(repo_root: Path) -> WorkstreamsConfig | None:
    """Load workstreams from repo config without requiring RepoConfig.workstreams."""
    for name in REPO_CONFIG_NAMES:
        path = repo_root / name
        if not path.exists():
            continue
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            return None
        return load_workstreams_config(raw)
    return None


def cmd_workstream_list(args: argparse.Namespace) -> int:
    workspace = _resolve_workspace(args)
    repo = find_repo_config(workspace)
    if repo is None:
        print(json.dumps({"enabled": False, "items": []}, indent=2))
        return 0
    config = load_repo_workstreams(repo.repo_root)
    if config is None:
        print(json.dumps({"enabled": False, "items": []}, indent=2))
        return 0
    print(workstreams_status(repo, config))
    return 0


def cmd_workstream_run(args: argparse.Namespace) -> int:
    workspace = _resolve_workspace(args)
    repo = find_repo_config(workspace)
    if repo is None:
        print("error: no .agent-fleet.yaml found", file=sys.stderr)
        return 1
    config = load_repo_workstreams(repo.repo_root)
    if config is None:
        print("error: no workstreams configured in .agent-fleet.yaml", file=sys.stderr)
        return 1

    if args.all:
        item_ids = config.ids()
    elif args.id:
        item_ids = args.id
    else:
        print("error: pass workstream id(s) or --all", file=sys.stderr)
        return 1

    try:
        if args.dry_run:
            tasks = build_workstream_tasks(
                repo=repo,
                config=config,
                item_ids=item_ids,
                parallel=bool(args.parallel),
            )
            print(json.dumps(tasks, indent=2, default=str))
            return 0

        results = run_workstreams(
            repo=repo,
            config=config,
            item_ids=item_ids,
            parallel=bool(args.parallel),
            fleet_config_path=args.config,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps([r.__dict__ for r in results], indent=2, default=str))
    failed = [r for r in results if r.status != "completed"]
    return 1 if failed else 0


def cmd_workstream_harvest(args: argparse.Namespace) -> int:
    workspace = _resolve_workspace(args)
    repo = find_repo_config(workspace)
    if repo is None:
        print("error: no .agent-fleet.yaml found", file=sys.stderr)
        return 1

    worktree = Path(args.worktree).expanduser().resolve()
    base = args.base or repo.default_branch
    try:
        if args.dry_run:
            plan = plan_harvest(
                repo_root=repo.repo_root,
                worktree_path=worktree,
                target_branch=args.target,
                base_branch=base,
            )
            print(
                json.dumps(
                    {
                        "dry_run": True,
                        "target_branch": plan.target_branch,
                        "source_sha": plan.source_sha,
                        "source_branch": plan.source_branch,
                        "base_branch": plan.base_branch,
                    },
                    indent=2,
                )
            )
            return 0

        sha = harvest_worktree(
            repo_root=repo.repo_root,
            worktree_path=worktree,
            target_branch=args.target,
            base_branch=base,
        )
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({"target_branch": args.target, "merge_commit": sha}, indent=2))
    return 0


def build_workstream_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog or "agent-fleet workstream",
        description="Run repo-defined workstream batches from .agent-fleet.yaml",
    )
    parser.add_argument("--workspace", help="Repo path (default: cwd)")
    parser.add_argument("--config", help="Path to fleet.yaml")
    ws_sub = parser.add_subparsers(dest="workstream_command", required=True)

    def _add_workspace_flags(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--workspace",
            help="Repo path (default: cwd or parent workstream --workspace)",
        )
        subparser.add_argument("--config", help="Path to fleet.yaml")

    list_p = ws_sub.add_parser("list", help="List configured workstreams")
    _add_workspace_flags(list_p)
    list_p.set_defaults(func=cmd_workstream_list)

    run_p = ws_sub.add_parser("run", help="Dispatch one or more workstreams")
    run_p.add_argument("id", nargs="*", help="Workstream id(s)")
    run_p.add_argument("--all", action="store_true", help="Run every configured workstream")
    run_p.add_argument(
        "--parallel",
        action="store_true",
        help="Dispatch concurrently (blocked when scopes overlap)",
    )
    run_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print task payloads without dispatching",
    )
    _add_workspace_flags(run_p)
    run_p.set_defaults(func=cmd_workstream_run)

    harvest_p = ws_sub.add_parser(
        "harvest",
        help="Merge a fleet-run worktree onto a feature branch",
    )
    harvest_p.add_argument("worktree", help="Path to fleet worktree")
    harvest_p.add_argument("--target", required=True, help="Target branch to merge into")
    harvest_p.add_argument("--base", help="Base ref when creating target branch")
    harvest_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show merge plan without checking out or merging",
    )
    _add_workspace_flags(harvest_p)
    harvest_p.set_defaults(func=cmd_workstream_harvest)
    return parser


def register_workstream_commands(sub: argparse._SubParsersAction) -> None:
    ws = sub.add_parser(
        "workstream",
        help="Run repo-defined workstream batches from .agent-fleet.yaml",
    )
    ws.add_argument("--workspace", help="Repo path (default: cwd)")
    ws.add_argument("--config", help="Path to fleet.yaml")
    ws_sub = ws.add_subparsers(dest="workstream_command", required=True)

    def _add_workspace_flags(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--workspace",
            help="Repo path (default: cwd or parent workstream --workspace)",
        )
        parser.add_argument("--config", help="Path to fleet.yaml")

    list_p = ws_sub.add_parser("list", help="List configured workstreams")
    _add_workspace_flags(list_p)
    list_p.set_defaults(func=cmd_workstream_list)

    run_p = ws_sub.add_parser("run", help="Dispatch one or more workstreams")
    run_p.add_argument("id", nargs="*", help="Workstream id(s)")
    run_p.add_argument("--all", action="store_true", help="Run every configured workstream")
    run_p.add_argument(
        "--parallel",
        action="store_true",
        help="Dispatch concurrently (blocked when scopes overlap)",
    )
    run_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print task payloads without dispatching",
    )
    _add_workspace_flags(run_p)
    run_p.set_defaults(func=cmd_workstream_run)

    harvest_p = ws_sub.add_parser(
        "harvest",
        help="Merge a fleet-run worktree onto a feature branch",
    )
    harvest_p.add_argument("worktree", help="Path to fleet worktree")
    harvest_p.add_argument("--target", required=True, help="Target branch to merge into")
    harvest_p.add_argument("--base", help="Base ref when creating target branch")
    harvest_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show merge plan without checking out or merging",
    )
    _add_workspace_flags(harvest_p)
    harvest_p.set_defaults(func=cmd_workstream_harvest)


def main(argv: list[str] | None = None) -> int:
    parser = build_workstream_parser(prog="python -m agent_fleet.workstreams")
    args = parser.parse_args(argv)
    return args.func(args)


def _resolve_workspace(args: argparse.Namespace) -> Path:
    return Path(args.workspace or Path.cwd()).resolve()
