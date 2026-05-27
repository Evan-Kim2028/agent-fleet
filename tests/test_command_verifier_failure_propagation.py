"""Bootstrap/verify failure classification + stderr propagation.

Regression guard for a v0.8.3 dispatch where the fix loop burned 4 attempts on
an environmental failure (lockfile drift breaking ``npm ci``) because:

  * ``command_verifier`` returned ``VerifySeverity.RETRY`` for bootstrap
    failures, so the runner kept replaying SYNTHESIZE+IMPLEMENT.
  * The failure message contained only the command string, so the agent's
    fix prompt could not see why the command exited non-zero.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agent_fleet.contracts.verify_result import VerifySeverity
from agent_fleet.integrations.command_verifier import CommandVerifier
from agent_fleet.repo import RepoConfig

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    return tmp_path


def test_bootstrap_failure_is_fatal(worktree: Path) -> None:
    repo = RepoConfig(
        repo_root=worktree,
        state_root=worktree,
        worktree_bootstrap_commands=["bash -c 'echo boot-stderr >&2; exit 7'"],
    )
    result = CommandVerifier(repo).check(worktree, persona="coder", changed_files=[], task_id=1)
    assert result.severity is VerifySeverity.FATAL


def test_bootstrap_failure_message_includes_stderr_and_exit_code(
    worktree: Path,
) -> None:
    repo = RepoConfig(
        repo_root=worktree,
        state_root=worktree,
        worktree_bootstrap_commands=["bash -c 'echo boot-stderr >&2; exit 7'"],
    )
    result = CommandVerifier(repo).check(worktree, persona="coder", changed_files=[], task_id=1)
    assert "exit=7" in result.message
    assert "boot-stderr" in result.message


def test_verify_failure_message_includes_stderr_and_exit_code(
    worktree: Path,
) -> None:
    repo = RepoConfig(
        repo_root=worktree,
        state_root=worktree,
        verify_commands=["bash -c 'echo test-failed >&2; exit 3'"],
    )
    result = CommandVerifier(repo).check(worktree, persona="coder", changed_files=[], task_id=1)
    assert result.severity is VerifySeverity.RETRY
    assert "exit=3" in result.message
    assert "test-failed" in result.message
