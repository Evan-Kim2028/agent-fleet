"""CLI for scheduled fleet dispatch."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent_fleet.repo import find_repo_config
from agent_fleet.schedule.watcher import ScheduleWatcher, run_schedule_once


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-fleet-schedule",
        description="Cron-based scheduled fleet dispatch",
    )
    parser.add_argument("--workspace", help="Repo path (default: cwd)")
    parser.add_argument("--config", help="Path to fleet.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    list_p = sub.add_parser("list", help="List configured schedules and next due times")
    list_p.set_defaults(func="list")

    tick_p = sub.add_parser("tick", help="Evaluate schedules once")
    tick_p.add_argument("--once", action="store_true", help="Alias for single evaluation")
    tick_p.set_defaults(func="tick")

    run_p = sub.add_parser("run", help="Manually fire one schedule by id")
    run_p.add_argument("--id", required=True, help="Schedule job id")
    run_p.set_defaults(func="run")

    args = parser.parse_args(argv)

    from agent_fleet.logging_config import configure_fleet_logging

    configure_fleet_logging()

    workspace = Path(args.workspace or Path.cwd()).resolve()
    repo = find_repo_config(workspace)
    if repo is None or repo.schedules is None or not repo.schedules.enabled:
        print(json.dumps({"enabled": False}, indent=2))
        return 1 if args.func != "list" else 0

    watcher = ScheduleWatcher(
        repo,
        repo.schedules,
        issue_dispatch_config=repo.issue_dispatch,
        fleet_config_path=args.config,
    )

    if args.func == "list":
        print(json.dumps({"jobs": watcher.list_jobs()}, indent=2))
        return 0

    if args.func == "run":
        results = run_schedule_once(
            workspace,
            fleet_config_path=args.config,
            force_job_id=args.id,
        )
        print(json.dumps(results, indent=2))
        return 0 if any(r.get("status") == "dispatched" for r in results) else 1

    results = run_schedule_once(workspace, fleet_config_path=args.config)
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
