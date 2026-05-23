"""Tests for git worktree isolation."""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent_fleet.repo import RepoConfig
from agent_fleet.worktree import prepare_task_workspace, should_isolate_worktree


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True)


def test_should_isolate_parallel_batch() -> None:
    repo = RepoConfig(repo_root=Path("/tmp/repo"), use_worktree=False)
    assert should_isolate_worktree(repo, batch_size=2, same_workspace_tasks=2) is True
    assert should_isolate_worktree(repo, batch_size=1, same_workspace_tasks=1) is False


def test_should_isolate_when_repo_flag_set() -> None:
    repo = RepoConfig(repo_root=Path("/tmp/repo"), use_worktree=True)
    assert should_isolate_worktree(repo, batch_size=1, same_workspace_tasks=1) is True


def test_prepare_task_workspace_creates_isolated_paths(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)
    base = tmp_path / "worktrees"
    repo = RepoConfig(
        repo_root=repo_path,
        use_worktree=True,
        worktree_base=base,
        default_branch="main",
    )

    first = prepare_task_workspace(repo, task_index=0, force_isolation=True)
    second = prepare_task_workspace(repo, task_index=1, force_isolation=True)

    assert first.isolated is True
    assert second.isolated is True
    assert first.path != second.path
    assert first.path.exists()
    assert second.path.exists()
    assert first.branch_name != second.branch_name

    first.teardown(keep=False)
    second.teardown(keep=False)
    assert not first.path.exists()
    assert not second.path.exists()


def test_prepare_task_workspace_keep_on_success(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)
    repo = RepoConfig(
        repo_root=repo_path,
        use_worktree=True,
        worktree_base=tmp_path / "worktrees",
    )
    task = prepare_task_workspace(repo, task_index=0, force_isolation=True)
    path = task.path
    task.teardown(keep=True)
    assert path.exists()
    task.teardown(keep=False)
