"""Tests for baseline re-gating of failing pytest verify commands.

Mirrors ``test_verify_autofix_ruff.py``'s guarantee for ``ruff check``:
pre-existing debt elsewhere in the tree must not fail a scoped task's
verify gate. There was no equivalent for pytest-based verify commands —
this fixed that (see ``_baseline_regate_pytest`` in ``agent_fleet/phases.py``).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from typing import TYPE_CHECKING

from agent_fleet.phases import run_verify_phases
from agent_fleet.repo import RepoConfig

if TYPE_CHECKING:
    from pathlib import Path

_PYTEST_CMD = f"{sys.executable} -m pytest -q"


def _git_init(tmpdir: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(tmpdir), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(tmpdir),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=str(tmpdir), check=True, capture_output=True
    )


def _git_commit_all(tmpdir: Path, message: str) -> None:
    subprocess.run(["git", "add", "."], cwd=str(tmpdir), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", message], cwd=str(tmpdir), check=True, capture_output=True
    )


def _make_repo(tmpdir: Path, verify_commands: list[str]) -> RepoConfig:
    repo = RepoConfig(repo_root=tmpdir)
    repo.verify_commands = verify_commands
    repo.worktree_bootstrap_commands = []
    repo.critical_path_prefixes = ()
    return repo


def test_baseline_regate_passes_when_failure_pre_existed(tmp_path: Path) -> None:
    """A test already broken before this task's diff must not fail verify."""
    _git_init(tmp_path)
    (tmp_path / "test_legacy.py").write_text(
        textwrap.dedent("""\
            def test_broken_before_this_task():
                assert False, "pre-existing failure unrelated to this diff"
            """),
        encoding="utf-8",
    )
    _git_commit_all(tmp_path, "seed: pre-existing broken test")

    # The task's own (unrelated, passing) change.
    (tmp_path / "test_feature.py").write_text(
        textwrap.dedent("""\
            def test_new_feature_works():
                assert 1 + 1 == 2
            """),
        encoding="utf-8",
    )

    repo = _make_repo(tmp_path, verify_commands=[_PYTEST_CMD])
    results = run_verify_phases(workspace=tmp_path, repo=repo, timeout_s=120)

    assert results, "expected a verify result"
    last = results[-1]
    assert last["passed"], last.get("detail")
    assert "pre-existing" in last.get("detail", "")

    # The stash/pop round trip must not have dropped the task's own change.
    assert (tmp_path / "test_feature.py").exists()
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        text=True,
    )
    assert "test_feature.py" in status.stdout, (
        "the task's uncommitted change must survive the baseline stash/pop round trip"
    )


def test_baseline_regate_still_fails_on_new_failure(tmp_path: Path) -> None:
    """A failure the task itself introduces must still fail verify."""
    _git_init(tmp_path)
    (tmp_path / "test_feature.py").write_text(
        textwrap.dedent("""\
            def test_passes_on_baseline():
                assert 1 + 1 == 2
            """),
        encoding="utf-8",
    )
    _git_commit_all(tmp_path, "seed: passing test")

    # The task's own change breaks the test that passed on the baseline.
    (tmp_path / "test_feature.py").write_text(
        textwrap.dedent("""\
            def test_passes_on_baseline():
                assert 1 + 1 == 3
            """),
        encoding="utf-8",
    )

    repo = _make_repo(tmp_path, verify_commands=[_PYTEST_CMD])
    results = run_verify_phases(workspace=tmp_path, repo=repo, timeout_s=120)

    assert results
    last = results[-1]
    assert not last["passed"], "a newly-introduced failure must not be waved through"

    # The task's own (still-broken, as intended) change must still be there.
    assert "assert 1 + 1 == 3" in (tmp_path / "test_feature.py").read_text(encoding="utf-8")


def test_baseline_regate_mixed_new_and_pre_existing_failures_still_fails(
    tmp_path: Path,
) -> None:
    """One new failure among several pre-existing ones must still fail the gate."""
    _git_init(tmp_path)
    (tmp_path / "test_legacy.py").write_text(
        textwrap.dedent("""\
            def test_broken_before_this_task():
                assert False
            """),
        encoding="utf-8",
    )
    (tmp_path / "test_feature.py").write_text(
        textwrap.dedent("""\
            def test_passes_on_baseline():
                assert 1 + 1 == 2
            """),
        encoding="utf-8",
    )
    _git_commit_all(tmp_path, "seed")

    # The task's change breaks a second, previously-passing test.
    (tmp_path / "test_feature.py").write_text(
        textwrap.dedent("""\
            def test_passes_on_baseline():
                assert 1 + 1 == 3
            """),
        encoding="utf-8",
    )

    repo = _make_repo(tmp_path, verify_commands=[_PYTEST_CMD])
    results = run_verify_phases(workspace=tmp_path, repo=repo, timeout_s=120)

    assert results
    assert not results[-1]["passed"]


def test_baseline_regate_skipped_for_non_pytest_command(tmp_path: Path) -> None:
    """Non-pytest, non-ruff commands are not baseline-regated at all."""
    _git_init(tmp_path)
    repo = _make_repo(tmp_path, verify_commands=["exit 1"])
    results = run_verify_phases(workspace=tmp_path, repo=repo, timeout_s=30)

    assert results
    assert not results[-1]["passed"]
    # No stash was created — a stray "exit 1" gate should never touch git state.
    status = subprocess.run(
        ["git", "stash", "list"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert status.stdout.strip() == ""
