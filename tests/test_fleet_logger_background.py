"""Tests for background/watcher FleetLogger paths."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from agent_fleet.observability.fleet_logger import FleetLogger, get_watcher_logger

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_watcher_logger_writes_jsonl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agent_fleet.observability.log._DEFAULT_RUNS_DIR",
        tmp_path,
    )
    import agent_fleet.observability.fleet_logger as fl_mod

    fl_mod._WATCHER_LOGGER = None

    logger = get_watcher_logger()
    logger.emit("admission.check", allowed=False, reason="max_parallel")

    path = tmp_path / "watcher.jsonl"
    assert path.is_file()
    line = json.loads(path.read_text(encoding="utf-8").strip())
    assert line["event"] == "admission.check"
    assert line["data"]["reason"] == "max_parallel"


def test_for_background_pr_loop_run_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agent_fleet.observability.log._DEFAULT_RUNS_DIR",
        tmp_path,
    )
    fleet_log = FleetLogger.for_background(run_id="pr-loop-42", persona="coder")
    with fleet_log.bind():
        fleet_log.emit("pr_loop.start", pr_number=42)

    path = tmp_path / "pr-loop-42.jsonl"
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8").strip())
    assert payload["event"] == "pr_loop.start"
    assert payload["persona"] == "coder"
