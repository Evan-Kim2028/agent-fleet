"""Regression tests for the fail-open changed-files detection bug.

``get_changed_files`` used to silently return ``[]`` whenever it could not
resolve a diff base (e.g. no ``origin`` remote), which made VERIFY report a
false "No changes detected -> OK" even though the branch had real, committed
changes. These tests pin:

  * committed changes on an origin-less repo are still detected (the exact
    reproduction of the bug);
  * a repo with an origin remote still works (no regression);
  * a genuinely empty diff is reported as determinate + empty, so VERIFY
    still returns OK;
  * a truly indeterminate repo (no commits at all, so there is nothing to
    resolve a base against) is reported as ``determinate=False``, and
    ``CommandVerifier.check`` fails closed (RETRY, not OK) in that case.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from agent_fleet.contracts.verify_result import VerifySeverity
from agent_fleet.integrations.command_verifier import CommandVerifier
from agent_fleet.repo import RepoConfig
from agent_fleet.verify_core import get_changed_files, get_changed_files_result

if TYPE_CHECKING:
    from pathlib import Path


def _git_init(tmpdir: Path, *, branch: str = "main") -> None:
    subprocess.run(
        ["git", "init", "-b", branch], cwd=str(tmpdir), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(tmpdir),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=str(tmpdir), check=True, capture_output=True
    )


def _commit_all(tmpdir: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=str(tmpdir), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", message], cwd=str(tmpdir), check=True, capture_output=True
    )


def test_committed_change_with_no_origin_remote_is_detected(tmp_path: Path) -> None:
    """Regression test for the exact reported bug: origin-less repo, committed
    change, must NOT silently return []."""
    _git_init(tmp_path)
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    _commit_all(tmp_path, "initial")

    (tmp_path / "feature.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(tmp_path, "agent change")

    files = get_changed_files(tmp_path)

    assert "feature.py" in files

    result = get_changed_files_result(tmp_path)
    assert result.determinate is True
    assert "feature.py" in result.files


def test_committed_change_with_origin_remote_still_detected(
    tmp_path: Path, tmp_path_factory  # noqa: ANN001
) -> None:
    """No-regression check: a repo WITH an origin remote still resolves the
    real changed files via the origin/<default> merge-base path."""
    origin = tmp_path_factory.mktemp("origin")
    _git_init(origin)
    (origin / "base.txt").write_text("base\n", encoding="utf-8")
    _commit_all(origin, "initial")

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(origin), str(clone)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(clone),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=str(clone), check=True, capture_output=True
    )

    (clone / "feature.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(clone, "agent change")

    files = get_changed_files(clone)
    assert "feature.py" in files

    result = get_changed_files_result(clone)
    assert result.determinate is True
    assert "feature.py" in result.files


def test_no_changes_is_determinate_and_empty(tmp_path: Path) -> None:
    """A branch with genuinely nothing new must report determinate=True with
    an empty file list, not be conflated with the indeterminate case.

    Uses an empty commit on top of the initial commit so HEAD^ (the fallback
    base for an origin-less, single-branch repo) genuinely has no diff --
    this is distinct from the very first commit, whose contents would always
    show up as "changed" relative to the empty tree.
    """
    _git_init(tmp_path)
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    _commit_all(tmp_path, "initial")
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "no-op"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    result = get_changed_files_result(tmp_path)

    assert result.determinate is True
    assert result.files == []
    assert get_changed_files(tmp_path) == []


def test_repo_with_no_commits_is_indeterminate(tmp_path: Path) -> None:
    """A git repo with zero commits has no HEAD, so no base-resolution
    strategy (origin, local default branch, HEAD^, empty-tree-of-HEAD) can
    resolve anything -- this must be reported as indeterminate, not "no
    changes"."""
    _git_init(tmp_path)

    result = get_changed_files_result(tmp_path)

    assert result.determinate is False
    assert result.files == []
    # The thin-wrapper API must not raise for existing callers, even here.
    assert get_changed_files(tmp_path) == []


def test_verify_ok_on_genuinely_empty_determinate_repo(tmp_path: Path) -> None:
    """VERIFY must keep the existing OK behavior when detection is
    determinate and the diff is genuinely empty."""
    _git_init(tmp_path)
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    _commit_all(tmp_path, "initial")
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "no-op"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    repo = RepoConfig(repo_root=tmp_path, state_root=tmp_path)
    repo.verify_commands = []
    repo.worktree_bootstrap_commands = []
    repo.critical_path_prefixes = ()

    result = CommandVerifier(repo).check(
        tmp_path, persona="coder", changed_files=[], task_id=1
    )

    assert result.severity is VerifySeverity.OK
    assert result.message == "No changes detected"


def test_verify_fails_closed_on_indeterminate_repo(tmp_path: Path) -> None:
    """VERIFY must NOT return a false OK when detection is indeterminate --
    this is the fail-closed contract for the fail-open bug."""
    _git_init(tmp_path)  # no commits at all: HEAD does not resolve

    repo = RepoConfig(repo_root=tmp_path, state_root=tmp_path)
    repo.verify_commands = []
    repo.worktree_bootstrap_commands = []
    repo.critical_path_prefixes = ()

    result = CommandVerifier(repo).check(
        tmp_path, persona="coder", changed_files=[], task_id=1
    )

    assert result.severity is VerifySeverity.RETRY
    assert "indeterminate" in result.message.lower()


def test_get_changed_files_signature_unchanged_for_existing_callers(tmp_path: Path) -> None:
    """Existing callers pass a bare worktree path and expect a plain list."""
    _git_init(tmp_path)
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    _commit_all(tmp_path, "initial")
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "no-op"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    result = get_changed_files(tmp_path)
    assert isinstance(result, list)
    assert result == []
