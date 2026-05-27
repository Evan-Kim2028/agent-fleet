"""Bootstrap commands emit structured worktree.bootstrap fleet events."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from agent_fleet.integrations.command_verifier import CommandVerifier
from agent_fleet.repo import RepoConfig

if TYPE_CHECKING:
    from pathlib import Path


def test_bootstrap_failure_emits_worktree_bootstrap_event(tmp_path: Path) -> None:
    repo = RepoConfig(
        repo_root=tmp_path,
        name="bootstrap-repo",
        worktree_bootstrap_commands=["false"],
        verify_commands=[],
    )
    events: list[tuple[str, dict]] = []

    def _capture(event: str, **payload: object) -> None:
        events.append((event, dict(payload)))

    with patch(
        "agent_fleet.integrations.command_verifier.emit_fleet_event",
        side_effect=_capture,
    ):
        result = CommandVerifier(repo).check(
            tmp_path,
            persona="coder",
            changed_files=[],
            task_id=42,
        )

    assert not result.passed
    bootstrap_events = [e for e in events if e[0] == "worktree.bootstrap"]
    assert len(bootstrap_events) == 1
    payload = bootstrap_events[0][1]
    assert payload["commands"] == ["false"]
    assert payload["exit_code"] != 0
    assert isinstance(payload["duration_s"], float)


def test_bootstrap_emits_once_for_multiple_commands(tmp_path: Path) -> None:
    """worktree.bootstrap fires exactly once per verifier.check() call,
    even when multiple bootstrap commands are configured."""
    repo = RepoConfig(
        repo_root=tmp_path,
        name="bootstrap-repo",
        worktree_bootstrap_commands=["true", "true"],
        verify_commands=[],
    )
    events: list[tuple[str, dict]] = []

    def _capture(event: str, **payload: object) -> None:
        events.append((event, dict(payload)))

    with patch(
        "agent_fleet.integrations.command_verifier.emit_fleet_event",
        side_effect=_capture,
    ):
        CommandVerifier(repo).check(
            tmp_path,
            persona="coder",
            changed_files=[],
            task_id=1,
        )

    bootstrap_events = [e for e in events if e[0] == "worktree.bootstrap"]
    assert len(bootstrap_events) == 1
    payload = bootstrap_events[0][1]
    assert payload["commands"] == ["true", "true"]
    assert payload["exit_code"] == 0
    assert payload["duration_s"] >= 0.0
