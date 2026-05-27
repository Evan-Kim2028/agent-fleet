"""Bootstrap commands emit structured worktree.bootstrap fleet events."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from agent_fleet.integrations.command_verifier import CommandVerifier
from agent_fleet.repo import RepoConfig


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
    assert bootstrap_events[0][1]["passed"] is False
    assert bootstrap_events[0][1]["command"] == "false"
    assert bootstrap_events[0][1]["exit_code"] != 0
