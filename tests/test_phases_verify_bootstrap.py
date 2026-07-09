"""run_verify_phases runs worktree_bootstrap_commands before persona verify.

code_review path must match CommandVerifier: bootstrap (e.g. symlink
frontend/node_modules) before lint/typecheck/test so tools like react-router
are on PATH.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from agent_fleet.phases import run_verify_phases
from agent_fleet.repo import RepoConfig

if TYPE_CHECKING:
    from pathlib import Path


def _repo(tmp_path: Path, *, bootstrap: list[str], verify: list[str]) -> RepoConfig:
    repo = RepoConfig(repo_root=tmp_path)
    repo.worktree_bootstrap_commands = bootstrap
    repo.verify_commands = verify
    return repo


def _ok(command: str) -> dict[str, Any]:
    return {
        "command": command,
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "passed": True,
        "detail": "ok",
    }


def test_bootstrap_runs_before_verify_commands(tmp_path: Path) -> None:
    order: list[str] = []

    def fake_shell(
        workspace: Path,
        command: str,
        *,
        timeout_s: int = 600,
        **_kw: object,
    ) -> dict[str, Any]:
        del workspace, timeout_s
        order.append(command)
        return _ok(command)

    repo = _repo(tmp_path, bootstrap=["echo boot"], verify=["echo verify"])
    with (
        patch("agent_fleet.phases.run_shell_verify", side_effect=fake_shell),
        patch("agent_fleet.phases.run_scoped_lint_command", side_effect=fake_shell),
    ):
        results = run_verify_phases(workspace=tmp_path, repo=repo, timeout_s=30)

    assert order == ["echo boot", "echo verify"]
    assert results[0]["command"] == "bootstrap: echo boot"
    assert results[0]["passed"] is True
    assert results[1]["passed"] is True


def test_bootstrap_failure_stops_before_verify(tmp_path: Path) -> None:
    order: list[str] = []

    def fake_shell(
        workspace: Path,
        command: str,
        *,
        timeout_s: int = 600,
        **_kw: object,
    ) -> dict[str, Any]:
        del workspace, timeout_s
        order.append(command)
        ok = command != "false"
        return {
            "command": command,
            "exit_code": 0 if ok else 1,
            "stdout": "",
            "stderr": "bootstrap boom",
            "passed": ok,
            "detail": "ok" if ok else "failed",
        }

    repo = _repo(tmp_path, bootstrap=["false"], verify=["echo should-not-run"])
    with (
        patch("agent_fleet.phases.run_shell_verify", side_effect=fake_shell),
        patch(
            "agent_fleet.phases.run_scoped_lint_command",
            side_effect=AssertionError("verify must not run after bootstrap fail"),
        ),
    ):
        results = run_verify_phases(workspace=tmp_path, repo=repo, timeout_s=30)

    assert order == ["false"]
    assert len(results) == 1
    assert results[0]["passed"] is False
    assert results[0]["command"] == "bootstrap: false"
    assert results[0]["detail"] == "failed"


def test_bootstrap_only_when_no_verify_commands(tmp_path: Path) -> None:
    """Empty verify_commands still runs bootstrap (parity with CommandVerifier)."""

    def fake_shell(
        workspace: Path,
        command: str,
        *,
        timeout_s: int = 600,
        **_kw: object,
    ) -> dict[str, Any]:
        del workspace, timeout_s
        return _ok(command)

    repo = _repo(tmp_path, bootstrap=["true"], verify=[])
    with patch("agent_fleet.phases.run_shell_verify", side_effect=fake_shell):
        results = run_verify_phases(workspace=tmp_path, repo=repo, timeout_s=30)
    assert len(results) == 1
    assert results[0]["command"] == "bootstrap: true"
    assert results[0]["passed"] is True


def test_no_bootstrap_no_verify_returns_empty(tmp_path: Path) -> None:
    repo = _repo(tmp_path, bootstrap=[], verify=[])
    assert run_verify_phases(workspace=tmp_path, repo=repo, timeout_s=30) == []
