"""Tests for cron-based scheduled dispatch."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import yaml

if TYPE_CHECKING:
    from pathlib import Path

from agent_fleet.schedule.config import load_schedule_config
from agent_fleet.schedule.cron import format_iso, is_due, next_fire_at, parse_iso
from agent_fleet.schedule.dispatch import (
    SCHEDULE_MARKER,
    build_issue_comment,
    synthetic_issue_number,
)
from agent_fleet.schedule.watcher import ScheduleWatcher, job_state


def test_load_schedule_config_parses_jobs(tmp_path: Path) -> None:
    raw = {
        "schedules": {
            "enabled": True,
            "poll_interval_s": 45,
            "jobs": [
                {
                    "id": "docs-daily",
                    "cron": "0 6 * * *",
                    "timezone": "UTC",
                    "dispatch": {
                        "kind": "issue",
                        "issue": 42,
                        "persona": "docs",
                        "note": "Sync docs",
                    },
                },
                {
                    "id": "audit",
                    "cron": "0 9 * * 1",
                    "dispatch": {
                        "kind": "task",
                        "goal": "Audit dependencies",
                        "persona": "backend",
                        "pipeline": "simple",
                    },
                },
            ],
        }
    }
    cfg = load_schedule_config(tmp_path, raw)
    assert cfg is not None
    assert cfg.enabled is True
    assert cfg.poll_interval_s == 45
    assert len(cfg.jobs) == 2
    assert cfg.jobs[0].dispatch.issue == 42
    assert cfg.jobs[1].dispatch.kind == "task"


def test_next_fire_at_advances_in_utc() -> None:
    after = datetime(2026, 5, 25, 10, 0, tzinfo=UTC)
    nxt = next_fire_at(cron="0 6 * * *", timezone="UTC", after=after)
    assert nxt > after
    assert nxt.hour == 6


def test_is_due_when_next_due_in_past() -> None:
    past = format_iso(datetime(2020, 1, 1, tzinfo=UTC))
    assert is_due(next_due_at=past, now=datetime(2026, 1, 1, tzinfo=UTC))


def test_build_issue_comment_includes_marker() -> None:
    from agent_fleet.issue_loop.config import IssueDispatchConfig
    from agent_fleet.schedule.config import ScheduleDispatchConfig, ScheduleJob

    job = ScheduleJob(
        id="docs-daily",
        cron="0 6 * * *",
        dispatch=ScheduleDispatchConfig(kind="issue", issue=42, persona="docs", note="hello"),
    )
    body = build_issue_comment(job, IssueDispatchConfig())
    assert "/agent --persona docs" in body
    assert "hello" in body
    assert SCHEDULE_MARKER in body


def test_synthetic_issue_number_is_negative_and_stable() -> None:
    a = synthetic_issue_number("docs-daily")
    b = synthetic_issue_number("docs-daily")
    c = synthetic_issue_number("other")
    assert a == b
    assert a < 0
    assert c != a


def test_schedule_watcher_dispatches_task_when_due(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".agent-fleet.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "test",
                "schedules": {
                    "enabled": True,
                    "jobs": [
                        {
                            "id": "audit",
                            "cron": "* * * * *",
                            "dispatch": {
                                "kind": "task",
                                "goal": "Run audit",
                                "persona": "coder",
                                "pipeline": "simple",
                            },
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    from agent_fleet.repo import find_repo_config

    repo = find_repo_config(repo_root)
    assert repo is not None
    assert repo.schedules is not None

    watcher = ScheduleWatcher(repo, repo.schedules)
    state: dict = {"schedules": {"audit": {"next_due_at": "2020-01-01T00:00:00Z"}}}

    with (
        patch("agent_fleet.schedule.watcher.spawn_task_dispatch", return_value=99999) as spawn,
        patch("agent_fleet.schedule.watcher.available_ram_gb", return_value=64.0),
    ):
        results = watcher.poll_once(state)

    assert any(r.get("status") == "dispatched" for r in results)
    spawn.assert_called_once()
    assert state["schedules"]["audit"]["next_due_at"] > "2020-01-01T00:00:00Z"
    assert state["in_flight"]


def test_schedule_watcher_defers_when_in_flight(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".agent-fleet.yaml").write_text(
        yaml.safe_dump(
            {
                "schedules": {
                    "enabled": True,
                    "jobs": [
                        {
                            "id": "audit",
                            "cron": "* * * * *",
                            "dispatch": {
                                "kind": "task",
                                "goal": "Run audit",
                                "persona": "coder",
                            },
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    from agent_fleet.repo import find_repo_config

    repo = find_repo_config(repo_root)
    assert repo is not None and repo.schedules is not None
    watcher = ScheduleWatcher(repo, repo.schedules)
    state: dict = {
        "schedules": {
            "audit": {
                "next_due_at": "2020-01-01T00:00:00Z",
                "in_flight": {"pid": 1, "started_at": "2026-01-01T00:00:00Z"},
            }
        }
    }

    with patch("agent_fleet.in_flight.pid_is_fleet_dispatch", return_value=True):
        results = watcher.poll_once(state)

    assert results == [{"job": "audit", "status": "schedule_in_flight"}]


def test_parse_iso_roundtrip() -> None:
    value = "2026-05-25T12:00:00Z"
    assert format_iso(parse_iso(value)) == value


def test_job_state_creates_nested_dict() -> None:
    state: dict = {}
    entry = job_state(state, "docs-daily")
    entry["next_due_at"] = "2026-01-01T00:00:00Z"
    assert state["schedules"]["docs-daily"]["next_due_at"] == "2026-01-01T00:00:00Z"
