"""Run a scheduled headless fleet task (no GitHub issue)."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from agent_fleet.cli_env import require_backend_env
from agent_fleet.config import load_fleet_config
from agent_fleet.dispatcher import FleetDispatcher
from agent_fleet.logging_config import configure_fleet_logging
from agent_fleet.repo import find_repo_config

logger = logging.getLogger(__name__)


def run_scheduled_task(
    *,
    job_id: str,
    goal: str,
    persona: str,
    pipeline: str,
    context: str,
    repo_root: Path,
    fleet_config_path: str | None = None,
) -> int:
    configure_fleet_logging()
    repo = find_repo_config(repo_root)
    if repo is None:
        logger.error("No .agent-fleet.yaml found under %s", repo_root)
        return 1

    fleet_config = load_fleet_config(fleet_config_path)
    if repo.personas_dir:
        fleet_config.personas_dir = repo.personas_dir
    if (code := require_backend_env(fleet_config)) is not None:
        return code

    logger.info(
        "Scheduled task dispatch job=%s persona=%s pipeline=%s",
        job_id,
        persona,
        pipeline,
    )
    dispatcher = FleetDispatcher(config=fleet_config)
    results = dispatcher.dispatch(
        goal=goal,
        context=context,
        persona=persona,
        workspace=str(repo_root),
        pipeline=pipeline,
    )
    print(json.dumps([r.__dict__ for r in results], indent=2, default=str))
    return 0 if results and results[0].status in {"completed", "merged"} else 1


def main() -> None:
    job_id = os.environ.get("SCHEDULE_JOB_ID", "")
    goal = os.environ.get("SCHEDULE_GOAL", "")
    persona = os.environ.get("SCHEDULE_PERSONA") or None
    pipeline = os.environ.get("SCHEDULE_PIPELINE") or None
    context = os.environ.get("SCHEDULE_CONTEXT", "")
    workspace = Path(os.environ.get("AGENT_FLEET_WORKSPACE", Path.cwd())).resolve()
    fleet_config_path = os.environ.get("AGENT_FLEET_CONFIG")

    if not job_id or not goal:
        print("SCHEDULE_JOB_ID and SCHEDULE_GOAL env vars required", file=sys.stderr)
        raise SystemExit(1)

    raise SystemExit(
        run_scheduled_task(
            job_id=job_id,
            goal=goal,
            persona=persona or "coder",
            pipeline=pipeline or "code_review",
            context=context,
            repo_root=workspace,
            fleet_config_path=fleet_config_path,
        )
    )


if __name__ == "__main__":
    main()
