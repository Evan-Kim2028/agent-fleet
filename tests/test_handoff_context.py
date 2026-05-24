"""Tests for redispatch handoff context injection."""

from __future__ import annotations

from agent_fleet.contracts.handoff import HandoffNote
from agent_fleet.handoff_context import apply_handoff_to_task
from agent_fleet.hooks import FleetTask


def test_apply_handoff_prepends_context() -> None:
    task = FleetTask(goal="fix bug", context="see auth.py", persona="coder")
    handoff = HandoffNote(
        failure_mode="expired",
        files_touched=("src/a.py",),
        stderr_tail="timeout",
        summary="try again",
        attempt_number=1,
    )
    merged = apply_handoff_to_task(task, handoff)
    assert "PREVIOUS ATTEMPT CONTEXT" in merged.context
    assert "see auth.py" in merged.context
    assert merged.goal == task.goal


def test_apply_handoff_noop_when_none() -> None:
    task = FleetTask(goal="x", context="y")
    assert apply_handoff_to_task(task, None) is task
