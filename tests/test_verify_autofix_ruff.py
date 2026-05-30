# ruff: noqa: TC002
"""Tests for auto-apply ruff check --fix in CommandVerifier (v0.8.4)."""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from agent_fleet.contracts.verify_result import VerifySeverity
from agent_fleet.integrations.command_verifier import CommandVerifier
from agent_fleet.phases import run_verify_phases
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


def test_no_autofix_for_non_ruff_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_verify_commands_for_persona_scoping() -> None:
    repo = RepoConfig(repo_root=Path("/tmp"))
    repo.verify_commands = ["ruff check ."]
    repo.persona_verify_commands = {"lakestore": ("ruff check packages/lakestore",)}

    assert repo.verify_commands_for("lakestore") == ["ruff check packages/lakestore"]
    # Personas without a scoped set fall back to the repo-wide commands.
    assert repo.verify_commands_for("gold") == ["ruff check ."]
    assert repo.verify_commands_for(None) == ["ruff check ."]


def test_run_verify_phases_persona_scope_skips_out_of_lane_debt(tmp_path: Path) -> None:
    """A scoped persona must not fail on pre-existing lint debt outside its lane."""
    lane = tmp_path / "lane"
    lane.mkdir()
    (lane / "clean.py").write_text("x = 1\n", encoding="utf-8")
    # Pre-existing, non-auto-fixable debt (F821) the persona neither owns nor can touch.
    (tmp_path / "other.py").write_text("x = undefined_name\n", encoding="utf-8")

    ruff = "uv run ruff check" if shutil.which("uv") else "ruff check"
    repo = RepoConfig(repo_root=tmp_path)
    repo.verify_commands = [f"{ruff} ."]
    repo.persona_verify_commands = {"lakestore": (f"{ruff} lane",)}

    scoped = run_verify_phases(workspace=tmp_path, repo=repo, timeout_s=120, persona="lakestore")
    assert scoped and scoped[-1]["passed"], scoped[-1].get("detail")

    repo_wide = run_verify_phases(workspace=tmp_path, repo=repo, timeout_s=120, persona="gold")
    assert repo_wide and not repo_wide[-1]["passed"]


def test_run_verify_phases_autofixes_inline_lint(tmp_path: Path) -> None:
    """Auto-fixable lint inside the lane is fixed once rather than failing the phase."""
    (tmp_path / "bad.py").write_text("import os\nimport abc\nimport sys\n", encoding="utf-8")

    ruff = "uv run ruff check" if shutil.which("uv") else "ruff check"
    repo = RepoConfig(repo_root=tmp_path)
    repo.verify_commands = [f"{ruff} --select I bad.py"]

    results = run_verify_phases(workspace=tmp_path, repo=repo, timeout_s=120)
    assert results and results[-1]["passed"], results[-1].get("detail")


def test_run_verify_phases_autofix_reverts_out_of_lane_edits(tmp_path: Path) -> None:
    """A repo-wide --fix must not leave edits on files outside the task's lane."""
    _git_init(tmp_path)
    lane = tmp_path / "lane"
    lane.mkdir()
    # Out-of-lane file with auto-fixable lint (I001) committed clean.
    out = tmp_path / "other.py"
    out.write_text("import os\nimport abc\nimport sys\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "seed"], cwd=str(tmp_path), check=True, capture_output=True
    )

    ruff = "uv run ruff check" if shutil.which("uv") else "ruff check"
    repo = RepoConfig(repo_root=tmp_path)
    # Repo-wide lint over a tree whose only debt lives outside the task's lane.
    repo.verify_commands = [f"{ruff} ."]

    run_verify_phases(
        workspace=tmp_path,
        repo=repo,
        timeout_s=120,
        allowed_paths=("lane/",),
    )

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        text=True,
    )
    assert "other.py" not in status.stdout, (
        f"auto-fix escaped the lane and edited other.py: {status.stdout!r}"
    )


def test_run_verify_phases_ignores_pre_existing_debt_outside_diff(tmp_path: Path) -> None:
    """Lint passes when the task's own file is clean, even if a sibling has debt."""
    _git_init(tmp_path)
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "clean.py").write_text("x = 1\n", encoding="utf-8")
    # Sibling debt (F821) the task never touches; committed into the lane.
    (pkg / "legacy.py").write_text("y = undefined_name\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "seed"], cwd=str(tmp_path), check=True, capture_output=True
    )
    # The task's own change: a fresh, clean file in the lane.
    (pkg / "feature.py").write_text("z = 2\n", encoding="utf-8")

    ruff = "uv run ruff check" if shutil.which("uv") else "ruff check"
    repo = RepoConfig(repo_root=tmp_path)
    repo.verify_commands = [f"{ruff} pkg"]

    results = run_verify_phases(
        workspace=tmp_path, repo=repo, timeout_s=120, allowed_paths=("pkg/",)
    )
    assert results and results[-1]["passed"], results[-1].get("detail")


def test_run_verify_phases_catches_debt_in_changed_file(tmp_path: Path) -> None:
    """Non-auto-fixable debt the task introduces in its own file still fails."""
    _git_init(tmp_path)
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "clean.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "seed"], cwd=str(tmp_path), check=True, capture_output=True
    )
    # The task's own change introduces non-auto-fixable debt (F821).
    (pkg / "feature.py").write_text("z = undefined_name\n", encoding="utf-8")

    ruff = "uv run ruff check" if shutil.which("uv") else "ruff check"
    repo = RepoConfig(repo_root=tmp_path)
    repo.verify_commands = [f"{ruff} pkg"]

    results = run_verify_phases(
        workspace=tmp_path, repo=repo, timeout_s=120, allowed_paths=("pkg/",)
    )
    assert results and not results[-1]["passed"]
