# ruff: noqa: TC002, TC003
"""Tests for auto-apply ruff check --fix in CommandVerifier (v0.8.4)."""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from agent_fleet.contracts.verify_result import VerifySeverity
from agent_fleet.integrations.command_verifier import CommandVerifier
from agent_fleet.repo import RepoConfig


def _make_repo(tmpdir: Path, verify_commands: list[str]) -> RepoConfig:
    repo = RepoConfig(repo_root=tmpdir)
    repo.verify_commands = verify_commands
    repo.worktree_bootstrap_commands = []
    repo.critical_path_prefixes = ()
    return repo


def _git_init(tmpdir: Path) -> None:
    """Set up a minimal git repo with an initial commit."""
    subprocess.run(["git", "init"], cwd=str(tmpdir), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(tmpdir),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(tmpdir),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=str(tmpdir),
        check=True,
        capture_output=True,
    )


def _write_unsorted_imports(path: Path) -> None:
    """Write a Python file with I001 (unsorted imports) that ruff --fix can auto-correct."""
    path.write_text(
        textwrap.dedent("""\
            import os
            import sys
            import abc
            """),
        encoding="utf-8",
    )


def test_ruff_autofix_resolves_i001(tmp_path: Path) -> None:
    """A file with unsorted imports passes after ruff --fix runs automatically."""
    import shutil

    _git_init(tmp_path)

    # Write a file with unsorted imports and commit it
    src_file = tmp_path / "bad_imports.py"
    _write_unsorted_imports(src_file)
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add bad file"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    # Rewrite as working-tree change so it's in the diff
    _write_unsorted_imports(src_file)

    ruff_cmd = "uv run ruff check ." if shutil.which("uv") else "ruff check ."

    repo = _make_repo(tmp_path, verify_commands=[ruff_cmd])
    verifier = CommandVerifier(repo)

    result = verifier.check(
        tmp_path,
        persona="coder",
        changed_files=[],
        task_id=1,
    )

    # After autofix, ruff check should pass (I001 is auto-fixable)
    assert result.severity == VerifySeverity.OK, (
        f"Expected OK after autofix but got {result.severity}: {result.message}"
    )


def test_ruff_autofix_event_emitted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """verify.autofix.applied event is emitted when ruff --fix is applied."""
    import shutil

    _git_init(tmp_path)

    src_file = tmp_path / "bad_imports.py"
    _write_unsorted_imports(src_file)
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add bad file"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    _write_unsorted_imports(src_file)

    ruff_cmd = "uv run ruff check ." if shutil.which("uv") else "ruff check ."

    emitted_events: list[dict[str, object]] = []

    import agent_fleet.integrations.command_verifier as cv_module

    def capture_emit(event: str, **kwargs: object) -> None:
        emitted_events.append({"event": event, **kwargs})

    monkeypatch.setattr(cv_module, "emit_fleet_event", capture_emit)

    repo = _make_repo(tmp_path, verify_commands=[ruff_cmd])
    verifier = CommandVerifier(repo)
    verifier.check(tmp_path, persona="coder", changed_files=[], task_id=1)

    autofix_events = [e for e in emitted_events if e["event"] == "verify.autofix.applied"]
    assert len(autofix_events) == 1, f"Expected 1 autofix event, got: {emitted_events}"
    # The event carries data either nested under "data" key or at top level
    ev_payload = autofix_events[0]
    nested = ev_payload.get("data")
    ev_data = nested if isinstance(nested, dict) else ev_payload
    assert "command" in ev_data
    assert "before_exit" in ev_data
    assert "after_exit" in ev_data


def test_no_autofix_for_non_ruff_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Autofix is not triggered for non-ruff verify commands."""
    _git_init(tmp_path)

    emitted_events: list[dict[str, object]] = []

    import agent_fleet.integrations.command_verifier as cv_module

    def capture_emit(event: str, **kwargs: object) -> None:
        emitted_events.append({"event": event, **kwargs})

    monkeypatch.setattr(cv_module, "emit_fleet_event", capture_emit)

    repo = _make_repo(tmp_path, verify_commands=["exit 1"])
    verifier = CommandVerifier(repo)
    result = verifier.check(tmp_path, persona="coder", changed_files=[], task_id=1)

    assert result.severity == VerifySeverity.RETRY
    autofix_events = [e for e in emitted_events if e["event"] == "verify.autofix.applied"]
    assert len(autofix_events) == 0, "Non-ruff commands should not trigger autofix"
