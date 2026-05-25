"""Tests for FIFO issue dispatch queue."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from agent_fleet.capacity import FleetCapacity, FleetCapacityGate
from agent_fleet.issue_loop.config import IssueDispatchConfig, IssueQueueConfig
from agent_fleet.issue_loop.queue import (
    QueueItem,
    SpawnDispatchFn,
    build_queue_comment,
    load_queue_items,
    poll_queue,
    queue_state,
    sync_queue_fingerprint,
)

if TYPE_CHECKING:
    from pathlib import Path

ISSUE_VIEW = {"title": "t", "body": ""}


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    queue_file = tmp_path / ".agent-fleet-queue.yaml"
    queue_file.write_text(
        """queue:
  - issue: 100
    persona: backend
  - issue: 101
    persona: frontend
  - issue: 102
    persona: data
""",
        encoding="utf-8",
    )
    return tmp_path


def _spawn_recorder(spawned: list[int]) -> SpawnDispatchFn:
    def spawn(*, issue_number: int, **_: object) -> int:
        spawned.append(issue_number)
        return 1000 + issue_number

    return spawn


def test_load_queue_items(repo_root: Path) -> None:
    config = IssueQueueConfig(enabled=True)
    items = load_queue_items(repo_root, config)
    assert len(items) == 3
    assert items[0] == QueueItem(issue=100, persona="backend")
    assert items[1].issue == 101


def test_fingerprint_reset_resets_head(repo_root: Path) -> None:
    path = repo_root / ".agent-fleet-queue.yaml"
    state: dict[str, Any] = {"queue": {"head": 2, "fingerprint": "stale"}}
    sync_queue_fingerprint(state, path)
    assert state["queue"]["head"] == 0
    assert state["queue"]["fingerprint"]


def test_dispatch_mode_advances_on_success(repo_root: Path) -> None:
    config = IssueQueueConfig(enabled=True, advance="dispatch")
    dispatch = IssueDispatchConfig()
    state: dict[str, Any] = {"in_flight": {}}
    gate = FleetCapacityGate(FleetCapacity(max_dispatches=8))
    spawned: list[int] = []

    with (
        patch("agent_fleet.issue_loop.queue._issue_is_open", return_value=True),
        patch("agent_fleet.issue_loop.queue.github_ops.issue_view", return_value=ISSUE_VIEW),
        patch("agent_fleet.issue_loop.queue.github_ops.issue_labels", return_value=[]),
    ):
        results, deferred = poll_queue(
            repo_root=repo_root,
            dispatch_config=dispatch,
            queue_config=config,
            state=state,
            capacity_gate=gate,
            spawn_dispatch=_spawn_recorder(spawned),
            available_ram_gb=64.0,
        )

    assert not deferred
    assert spawned == [100, 101, 102]
    assert queue_state(state)["head"] == 3
    assert results[0]["status"] == "queue_dispatched"


def test_dispatch_mode_respects_capacity_per_poll(repo_root: Path) -> None:
    config = IssueQueueConfig(enabled=True, advance="dispatch")
    dispatch = IssueDispatchConfig()
    state: dict[str, Any] = {"in_flight": {}}
    gate = FleetCapacityGate(FleetCapacity(max_dispatches=2))
    spawned: list[int] = []

    with (
        patch("agent_fleet.issue_loop.queue._issue_is_open", return_value=True),
        patch("agent_fleet.issue_loop.queue.github_ops.issue_view", return_value=ISSUE_VIEW),
        patch("agent_fleet.issue_loop.queue.github_ops.issue_labels", return_value=[]),
    ):
        poll_queue(
            repo_root=repo_root,
            dispatch_config=dispatch,
            queue_config=config,
            state=state,
            capacity_gate=gate,
            spawn_dispatch=_spawn_recorder(spawned),
            available_ram_gb=64.0,
        )
        poll_queue(
            repo_root=repo_root,
            dispatch_config=dispatch,
            queue_config=config,
            state=state,
            capacity_gate=gate,
            spawn_dispatch=_spawn_recorder(spawned),
            available_ram_gb=64.0,
        )

    assert spawned == [100, 101]
    assert queue_state(state)["head"] == 2


def test_complete_mode_waits_for_in_flight(repo_root: Path) -> None:
    config = IssueQueueConfig(enabled=True, advance="complete")
    dispatch = IssueDispatchConfig()
    state: dict[str, Any] = {"in_flight": {}}
    gate = FleetCapacityGate(FleetCapacity(max_dispatches=8))
    spawned: list[int] = []

    with (
        patch("agent_fleet.issue_loop.queue._issue_is_open", return_value=True),
        patch("agent_fleet.issue_loop.queue.github_ops.issue_view", return_value=ISSUE_VIEW),
        patch("agent_fleet.issue_loop.queue.github_ops.issue_labels", return_value=[]),
    ):
        poll_queue(
            repo_root=repo_root,
            dispatch_config=dispatch,
            queue_config=config,
            state=state,
            capacity_gate=gate,
            spawn_dispatch=_spawn_recorder(spawned),
            available_ram_gb=64.0,
        )
        assert queue_state(state)["waiting_index"] == 0
        assert queue_state(state)["head"] == 0

        poll_queue(
            repo_root=repo_root,
            dispatch_config=dispatch,
            queue_config=config,
            state=state,
            capacity_gate=gate,
            spawn_dispatch=_spawn_recorder(spawned),
            available_ram_gb=64.0,
        )
        assert queue_state(state)["head"] == 0

        state["in_flight"].pop("100", None)
        poll_queue(
            repo_root=repo_root,
            dispatch_config=dispatch,
            queue_config=config,
            state=state,
            capacity_gate=gate,
            spawn_dispatch=_spawn_recorder(spawned),
            available_ram_gb=64.0,
        )

    assert queue_state(state)["head"] == 1


def test_head_of_line_blocks_on_capacity(repo_root: Path) -> None:
    config = IssueQueueConfig(enabled=True, advance="dispatch")
    dispatch = IssueDispatchConfig()
    in_flight = {
        str(i): [{"pid": i, "persona": "backend", "visual_audit": False}] for i in range(8)
    }
    state: dict[str, Any] = {"in_flight": in_flight}
    gate = FleetCapacityGate(FleetCapacity(max_dispatches=8))
    spawned: list[int] = []

    with (
        patch("agent_fleet.issue_loop.queue._issue_is_open", return_value=True),
        patch("agent_fleet.issue_loop.queue.github_ops.issue_view", return_value=ISSUE_VIEW),
        patch("agent_fleet.issue_loop.queue.github_ops.issue_labels", return_value=[]),
    ):
        _results, deferred = poll_queue(
            repo_root=repo_root,
            dispatch_config=dispatch,
            queue_config=config,
            state=state,
            capacity_gate=gate,
            spawn_dispatch=_spawn_recorder(spawned),
            available_ram_gb=64.0,
        )

    assert deferred
    assert spawned == []
    assert queue_state(state)["head"] == 0


def test_build_queue_comment_includes_marker() -> None:
    dispatch = IssueDispatchConfig()
    body = build_queue_comment(QueueItem(issue=1, persona="backend", note="do thing"), dispatch)
    assert "/agent --persona backend" in body
    assert "do thing" in body
    assert dispatch.comment_marker in body
