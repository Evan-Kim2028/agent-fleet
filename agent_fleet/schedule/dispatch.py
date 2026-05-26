"""Spawn scheduled dispatches."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.issue_loop.config import IssueDispatchConfig
    from agent_fleet.schedule.config import ScheduleDispatchConfig, ScheduleJob


SCHEDULE_MARKER = "<!-- agent-fleet-schedule -->"


def build_issue_comment(job: ScheduleJob, dispatch_config: IssueDispatchConfig) -> str:
    dispatch = job.dispatch
    lines = [f"/agent --persona {dispatch.persona}"]
    if dispatch.note:
        lines.extend(["", dispatch.note])
    lines.extend(
        [
            "",
            f"Scheduled dispatch `{job.id}` ({job.cron}, {job.timezone}).",
            dispatch_config.comment_marker,
            SCHEDULE_MARKER,
        ]
    )
    return "\n".join(lines)


def spawn_issue_dispatch(
    *,
    issue_number: int,
    comment_body: str,
    persona: str,
    repo_root: Path,
) -> int | None:
    env = os.environ.copy()
    env["ISSUE_NUMBER"] = str(issue_number)
    env["COMMENT_BODY"] = comment_body
    env["PERSONA"] = persona
    env["AGENT_FLEET_WORKSPACE"] = str(repo_root)
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent_fleet.issue_loop.dispatch"],
        env=env,
        cwd=str(repo_root),
        start_new_session=True,
    )
    return proc.pid


def spawn_task_dispatch(
    *,
    job_id: str,
    dispatch: ScheduleDispatchConfig,
    repo_root: Path,
    fleet_config_path: str | None = None,
) -> int | None:
    env = os.environ.copy()
    env["SCHEDULE_JOB_ID"] = job_id
    env["SCHEDULE_GOAL"] = dispatch.goal
    env["SCHEDULE_PERSONA"] = dispatch.persona
    env["SCHEDULE_PIPELINE"] = dispatch.pipeline
    env["SCHEDULE_CONTEXT"] = dispatch.context
    env["AGENT_FLEET_WORKSPACE"] = str(repo_root)
    if fleet_config_path:
        env["AGENT_FLEET_CONFIG"] = fleet_config_path
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent_fleet.schedule.task_dispatch"],
        env=env,
        cwd=str(repo_root),
        start_new_session=True,
    )
    return proc.pid


def synthetic_issue_number(job_id: str) -> int:
    """Stable negative issue key for headless task schedules in capacity tracking."""
    return -(abs(hash(job_id)) % 900_000 + 1)
