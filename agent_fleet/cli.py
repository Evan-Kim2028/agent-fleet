#!/usr/bin/env python3
"""CLI for agent_fleet."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from agent_fleet.backends import make_backend
from agent_fleet.config import load_fleet_config
from agent_fleet.dispatcher import dispatch_tasks
from agent_fleet.personas import YamlPersonaResolver
from agent_fleet.repo import find_repo_config
from agent_fleet.runner import run_full_pipeline


def cmd_run(args: argparse.Namespace) -> int:
    config = load_fleet_config(args.config) if args.config else load_fleet_config()
    backend_name = config.default_backend.lower()
    if backend_name == "cursor" and not os.environ.get("CURSOR_API_KEY"):
        print("error: CURSOR_API_KEY is not set", file=sys.stderr)
        return 1
    if backend_name == "kimi" and not os.environ.get("KIMI_API_KEY"):
        print("error: KIMI_API_KEY is not set (Kimi Code subscription)", file=sys.stderr)
        return 1

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
        return 0 if result.outcome == "completed" else 1

    results = dispatch_tasks(
        goal=args.goal,
        context=args.context,
        persona=args.persona,
        workspace=str(workspace),
        pipeline=args.pipeline,
        config_path=args.config,
    )
    print(json.dumps([r.__dict__ for r in results], indent=2, default=str))
    return 0 if results and results[0].status == "completed" else 1


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
    run_p.add_argument("--pipeline", default="simple", help="simple | code_review | full")
    run_p.set_defaults(func=cmd_run)

    personas_p = sub.add_parser("personas", help="List personas")
    personas_p.add_argument("--workspace", help="Repo path (for repo-local personas)")
    personas_p.set_defaults(func=cmd_personas)

    init_p = sub.add_parser("init", help="Create .agent-fleet.yaml in a repo")
    init_p.add_argument("path", nargs="?", help="Repo path")
    init_p.add_argument("--force", action="store_true")
    init_p.set_defaults(func=cmd_init)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
