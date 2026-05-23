"""Tests for code_review publish / PR creation."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

if TYPE_CHECKING:
    from pathlib import Path

from agent_fleet.code_review.publish import (
    commits_ahead_of_base,
    publish_fleet_branch,
    push_branch_if_ahead,
)
from agent_fleet.repo import RepoConfig


def test_commits_ahead_uses_origin_branch_when_present(tmp_path: Path) -> None:
    def fake_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        result = MagicMock()
        range_spec = cmd[3] if len(cmd) > 3 else ""
        if range_spec == "origin/main..origin/fleet/task-0":
            result.returncode = 0
            result.stdout = "3\n"
        else:
            result.returncode = 1
            result.stdout = ""
        return result

    with patch("agent_fleet.code_review.publish.subprocess.run", side_effect=fake_run):
        assert commits_ahead_of_base(tmp_path, "fleet/task-0", "main") == 3


def test_push_branch_if_ahead_pushes_when_head_is_ahead(tmp_path: Path) -> None:
    push_cmds: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        result = MagicMock()
        range_spec = cmd[3] if len(cmd) > 3 else ""
        if cmd[:3] == ["git", "rev-list", "--count"]:
            result.returncode = 0
            result.stdout = "2\n" if range_spec == "origin/fleet/x..HEAD" else "0\n"
        elif cmd[:2] == ["git", "push"]:
            push_cmds.append(cmd)
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        else:
            result.returncode = 1
        return result

    with patch("agent_fleet.code_review.publish.subprocess.run", side_effect=fake_run):
        assert push_branch_if_ahead(tmp_path, "fleet/x") is True
    assert push_cmds and push_cmds[0][-1] == "HEAD:fleet/x"


def test_push_branch_if_ahead_pushes_when_remote_branch_missing(tmp_path: Path) -> None:
    push_cmds: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        result = MagicMock()
        if cmd[:3] == ["git", "rev-list", "--count"]:
            result.returncode = 1
            result.stdout = ""
        elif cmd[:2] == ["git", "push"]:
            push_cmds.append(cmd)
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        else:
            result.returncode = 1
        return result

    with patch("agent_fleet.code_review.publish.subprocess.run", side_effect=fake_run):
        assert push_branch_if_ahead(tmp_path, "fleet/new") is True
    assert push_cmds


def test_publish_creates_pr_when_branch_already_pushed(tmp_path: Path) -> None:
    repo = RepoConfig(
        repo_root=tmp_path,
        name="demo",
        default_branch="main",
        default_persona="coder",
    )

    def fake_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        result = MagicMock()
        result.stderr = ""
        if cmd[:2] == ["gh", "pr"]:
            if cmd[2] == "list":
                result.returncode = 0
                result.stdout = "[]"
            else:
                result.returncode = 0
                result.stdout = "https://github.com/org/demo/pull/99\n"
        elif cmd[:3] == ["git", "rev-list", "--count"]:
            range_spec = cmd[3] if len(cmd) > 3 else ""
            result.returncode = 0
            if range_spec == "origin/main..origin/fleet/task-0-abc":
                result.stdout = "1\n"
            else:
                result.stdout = "0\n"
        else:
            result.returncode = 0
            result.stdout = ""
        return result

    with (
        patch("agent_fleet.code_review.publish.github_ops.commit_and_push", return_value=False),
        patch("agent_fleet.code_review.publish.subprocess.run", side_effect=fake_run),
        patch("agent_fleet.code_review.publish.push_branch_if_ahead", return_value=False),
    ):
        pr_number = publish_fleet_branch(
            worktree=tmp_path,
            branch="fleet/task-0-abc",
            repo=repo,
            task_goal="Add gold sanity check",
            persona="gold",
        )

    assert pr_number == 99
