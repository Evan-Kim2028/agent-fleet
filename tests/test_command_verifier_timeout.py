"""CommandVerifier must bound hung bootstrap/verify commands.

A missing timeout wedges the dispatch, holds the admission slot, and (with
shell=True) leaves pytest/uv grandchildren running after the shell is killed.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from agent_fleet.contracts.verify_result import VerifySeverity
from agent_fleet.integrations.command_verifier import CommandVerifier
from agent_fleet.repo import RepoConfig, load_repo_config

_SLEEP_S = 15
_TIMEOUT_S = 1
_ELAPSED_SLACK_S = 5


def _pid_is_live_non_zombie(pid: int) -> bool:
    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    for line in text.splitlines():
        if line.startswith("State:"):
            return "zombie" not in line.lower()
    return True


def _hang_python(*, stdout: str = "partial-out", stderr: str = "partial-err") -> str:
    return (
        f"{sys.executable} -c "
        f'"import sys, time; '
        f"print({stdout!r}, flush=True); "
        f"print({stderr!r}, file=sys.stderr, flush=True); "
        f'time.sleep({_SLEEP_S})"'
    )


def test_verify_timeout_s_defaults_to_600() -> None:
    repo = RepoConfig(repo_root=Path("/tmp/unused-verify-timeout"))
    assert repo.verify_timeout_s == 600


def test_load_repo_config_parses_verify_timeout_s(tmp_path: Path) -> None:
    cfg = tmp_path / ".agent-fleet.yaml"
    cfg.write_text("name: timed\nverify_timeout_s: 12\n", encoding="utf-8")
    repo = load_repo_config(cfg)
    assert repo.verify_timeout_s == 12


def test_load_repo_config_omitted_verify_timeout_s_defaults_to_600(tmp_path: Path) -> None:
    cfg = tmp_path / ".agent-fleet.yaml"
    cfg.write_text("name: untimed\n", encoding="utf-8")
    repo = load_repo_config(cfg)
    assert repo.verify_timeout_s == 600


def test_hanging_verify_command_returns_retry_not_fatal(tmp_path: Path) -> None:
    repo = RepoConfig(
        repo_root=tmp_path,
        state_root=tmp_path,
        verify_commands=[_hang_python()],
        verify_timeout_s=_TIMEOUT_S,
    )
    t0 = time.monotonic()
    result = CommandVerifier(repo).check(tmp_path, persona="coder", changed_files=[], task_id=1)
    elapsed = time.monotonic() - t0
    assert elapsed < _ELAPSED_SLACK_S
    assert result.severity is VerifySeverity.RETRY
    assert not result.passed


def test_bootstrap_timeout_is_fatal(tmp_path: Path) -> None:
    repo = RepoConfig(
        repo_root=tmp_path,
        state_root=tmp_path,
        worktree_bootstrap_commands=[_hang_python()],
        verify_commands=["true"],
        verify_timeout_s=_TIMEOUT_S,
    )
    t0 = time.monotonic()
    result = CommandVerifier(repo).check(tmp_path, persona="coder", changed_files=[], task_id=1)
    elapsed = time.monotonic() - t0
    assert elapsed < _ELAPSED_SLACK_S
    assert result.severity is VerifySeverity.FATAL
    assert "timed out" in result.message.lower()


def test_timeout_preserves_partial_stdout_and_stderr(tmp_path: Path) -> None:
    repo = RepoConfig(
        repo_root=tmp_path,
        state_root=tmp_path,
        verify_commands=[_hang_python()],
        verify_timeout_s=_TIMEOUT_S,
    )
    result = CommandVerifier(repo).check(tmp_path, persona="coder", changed_files=[], task_id=1)
    failed = [c for c in result.checks if not c["passed"]]
    assert failed
    assert "partial-out" in failed[0]["stdout_tail"]
    assert "partial-err" in failed[0]["stderr_tail"]


def test_timeout_kills_grandchild_sleep(tmp_path: Path) -> None:
    """shell=True must not leave a background sleep running after timeout."""
    cmd = f"echo $$ > parent.pid; sleep {_SLEEP_S} & echo $! > child.pid; wait"
    repo = RepoConfig(
        repo_root=tmp_path,
        state_root=tmp_path,
        verify_commands=[cmd],
        verify_timeout_s=_TIMEOUT_S,
    )
    t0 = time.monotonic()
    result = CommandVerifier(repo).check(tmp_path, persona="coder", changed_files=[], task_id=1)
    elapsed = time.monotonic() - t0
    assert elapsed < _ELAPSED_SLACK_S
    assert result.severity is VerifySeverity.RETRY

    child_pid = int((tmp_path / "child.pid").read_text(encoding="utf-8").strip())
    parent_pid = int((tmp_path / "parent.pid").read_text(encoding="utf-8").strip())
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _pid_is_live_non_zombie(child_pid) and not _pid_is_live_non_zombie(parent_pid):
            break
        time.sleep(0.05)
    else:
        raise AssertionError(
            f"orphans still live after timeout: parent={parent_pid} "
            f"child={child_pid} parent_alive={_pid_is_live_non_zombie(parent_pid)} "
            f"child_alive={_pid_is_live_non_zombie(child_pid)}"
        )


def test_fast_verify_command_still_passes_with_timeout(tmp_path: Path) -> None:
    repo = RepoConfig(
        repo_root=tmp_path,
        state_root=tmp_path,
        verify_commands=["true"],
        verify_timeout_s=_TIMEOUT_S,
    )
    result = CommandVerifier(repo).check(tmp_path, persona="coder", changed_files=[], task_id=1)
    assert result.severity is VerifySeverity.OK
