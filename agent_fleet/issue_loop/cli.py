"""CLI entry points for issue dispatch and combined watcher."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agent_fleet.issue_loop import queue as issue_queue
from agent_fleet.issue_loop.dispatch import run_issue_dispatch
from agent_fleet.issue_loop.watcher import CombinedWatcher, IssueLoopWatcher, run_watcher_once
from agent_fleet.repo import RepoConfig, find_repo_config


def _watcher_enabled(repo: RepoConfig | None) -> bool:
    if repo is None:
        return False
    return bool(
        (repo.issue_dispatch is not None and repo.issue_dispatch.enabled)
        or (repo.schedules is not None and repo.schedules.enabled)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-fleet-watch",
        description="Issue comment dispatch, schedules, and PR loop watcher",
    )
    parser.add_argument("--workspace", help="Repo path (default: cwd)")
    parser.add_argument("--config", help="Path to fleet.yaml")
    parser.add_argument("--once", action="store_true", help="Poll once and exit")
    parser.add_argument(
        "--issues-only",
        action="store_true",
        help="Run issue dispatch loop only (skip PR loop and schedules)",
    )
    parser.add_argument("--issue", type=int, help="Run one issue dispatch")
    parser.add_argument("--persona", help="Persona for --issue dispatch")
    parser.add_argument("--comment", default="", help="Trigger comment for --issue dispatch")
    parser.add_argument(
        "--queue-status",
        action="store_true",
        help="Print FIFO queue status from .agent-fleet-queue.yaml and exit",
    )
    args = parser.parse_args(argv)

    from agent_fleet.logging_config import configure_fleet_logging

    configure_fleet_logging()

    workspace = Path(args.workspace or Path.cwd()).resolve()

    if args.queue_status:
        repo = find_repo_config(workspace)
        if (
            repo is None
            or repo.issue_dispatch is None
            or not repo.issue_dispatch.queue
            or not repo.issue_dispatch.queue.enabled
        ):
            print(json.dumps({"enabled": False}, indent=2))
            return 0
        from agent_fleet.state import load_state, state_path

        state_file = state_path(repo.repo_root)
        state = load_state(state_file)
        print(
            json.dumps(
                issue_queue.queue_status(
                    repo.repo_root,
                    repo.issue_dispatch.queue,
                    state,
                ),
                indent=2,
            )
        )
        return 0

    if args.issue is not None:
        if not args.persona:
            print("error: --persona required with --issue", file=sys.stderr)
            return 1
        return run_issue_dispatch(
            issue_number=args.issue,
            comment_body=args.comment or f"/agent --persona {args.persona}",
            repo_root=workspace,
            persona=args.persona,
            fleet_config_path=args.config,
        )

    if args.issues_only:
        if args.once:
            print(json.dumps(run_watcher_once(workspace), indent=2))
            return 0
        repo = find_repo_config(workspace)
        if repo is None or repo.issue_dispatch is None or not repo.issue_dispatch.enabled:
            print("error: issue_dispatch.enabled not set in .agent-fleet.yaml", file=sys.stderr)
            return 1
        IssueLoopWatcher(repo, repo.issue_dispatch).run_forever()
        return 0

    repo = find_repo_config(workspace)
    if not _watcher_enabled(repo):
        print(
            "error: enable issue_dispatch or schedules in .agent-fleet.yaml",
            file=sys.stderr,
        )
        return 1

    watcher = CombinedWatcher(
        repo,
        issue_config=repo.issue_dispatch,
        pr_loop_config=repo.pr_loop,
        schedule_config=repo.schedules,
        fleet_config_path=args.config,
    )
    if args.once:
        print(json.dumps(watcher.poll_once(), indent=2))
        return 0
    watcher.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
