"""Isolated lane worktrees: create, reuse, and never destroy."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from agent_fleet.fleet_ops.worktree import (
    branch_exists,
    default_worktree_path,
    ensure_lane_worktree,
    find_worktree_for_branch,
    list_worktrees,
    sanitize_component,
    worktree_root,
)

if TYPE_CHECKING:
    from pathlib import Path


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "lake-of-rage"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "l@example.com")
    _git(root, "config", "user.name", "L")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "base")
    return root


# ------------------------------------------------------------------ sanitising


def test_sanitize_component_strips_path_separators() -> None:
    # A lane name is operator input; it must not escape its directory.
    assert sanitize_component("../../etc") == "etc"
    assert sanitize_component("a/b") == "a-b"
    assert sanitize_component("") == "lane"
    assert sanitize_component("   ") == "lane"


def test_default_path_matches_the_bash_driver_layout(tmp_path: Path) -> None:
    repo = tmp_path / "lake-of-rage"
    repo.mkdir()
    path = default_worktree_path(repo, "movers", parent=tmp_path)
    assert path.name == "lake-of-rage-wt-fb-movers"


# ------------------------------------------------------------------- creating


def test_worktree_is_created_from_base(repo: Path, tmp_path: Path) -> None:
    result = ensure_lane_worktree(repo, lane="movers", parent=tmp_path / "wt")
    assert result.created is True
    assert result.path.is_dir()
    assert result.branch == "fb/movers"
    assert _git(result.path, "rev-parse", "--abbrev-ref", "HEAD") == "fb/movers"


def test_an_existing_worktree_is_reused_not_recreated(repo: Path, tmp_path: Path) -> None:
    """A lane that was interrupted usually has real work; it must be adopted."""
    first = ensure_lane_worktree(repo, lane="movers", parent=tmp_path / "wt")
    (first.path / "work.py").write_text("x = 1\n", encoding="utf-8")

    second = ensure_lane_worktree(repo, lane="movers", parent=tmp_path / "wt")
    assert second.created is False
    assert second.path == first.path
    # The interrupted work survived.
    assert (second.path / "work.py").is_file()


def test_an_existing_branch_is_attached_not_recreated(repo: Path, tmp_path: Path) -> None:
    _git(repo, "branch", "fb/movers")
    result = ensure_lane_worktree(repo, lane="movers", parent=tmp_path / "wt")
    assert result.created is True
    assert result.branch == "fb/movers"
    assert "attached" in result.reason


def test_explicit_branch_overrides_the_default(repo: Path, tmp_path: Path) -> None:
    result = ensure_lane_worktree(repo, lane="movers", branch="dq1d/movers", parent=tmp_path / "wt")
    assert result.branch == "dq1d/movers"


def test_missing_repo_path_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="repo path does not exist"):
        ensure_lane_worktree(tmp_path / "nope", lane="x", parent=tmp_path)


def test_a_non_worktree_directory_is_never_deleted(tmp_path: Path) -> None:
    """The manager must not remove whatever the operator left at that path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "l@e.com")
    _git(repo, "config", "user.name", "L")
    (repo / "f").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")

    target = tmp_path / "wt" / "repo-wt-fb-movers"
    target.mkdir(parents=True)
    (target / "precious.txt").write_text("do not delete\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="not a worktree"):
        ensure_lane_worktree(repo, lane="movers", parent=tmp_path / "wt")
    assert (target / "precious.txt").is_file()


# ------------------------------------------------------------------- queries


def test_list_worktrees_reports_path_and_branch(repo: Path, tmp_path: Path) -> None:
    result = ensure_lane_worktree(repo, lane="movers", parent=tmp_path / "wt")
    entries = list_worktrees(worktree_root(repo))
    assert any(e.get("branch") == "fb/movers" and e.get("path") for e in entries)
    assert find_worktree_for_branch(worktree_root(repo), "fb/movers") == result.path


def test_find_worktree_returns_none_for_an_unknown_branch(repo: Path) -> None:
    assert find_worktree_for_branch(worktree_root(repo), "fb/nope") is None


def test_branch_exists(repo: Path) -> None:
    assert branch_exists(worktree_root(repo), "main")
    assert not branch_exists(worktree_root(repo), "fb/nope")
