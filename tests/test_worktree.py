"""Tests for git worktree isolation."""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent_fleet.repo import RepoConfig
from agent_fleet.worktree import (
    maybe_commit_recoverable_worktree,
    prepare_task_workspace,
    should_isolate_worktree,
    should_keep_task_worktree,
)


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
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


def test_should_keep_task_worktree() -> None:
    assert should_keep_task_worktree("completed") is True
    assert should_keep_task_worktree("review_changes_requested") is False
    assert should_keep_task_worktree("review_changes_requested", has_changes=True) is True
    assert should_keep_task_worktree("verify_failed", has_changes=True) is True
    assert should_keep_task_worktree("error", has_changes=True) is False


def test_prepare_task_workspace_uses_base_branch(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)
    subprocess.run(["git", "checkout", "-b", "feature/base"], cwd=repo_path, check=True)
    (repo_path / "base.txt").write_text("on base\n", encoding="utf-8")
    subprocess.run(["git", "add", "base.txt"], cwd=repo_path, check=True)
    subprocess.run(["git", "commit", "-m", "base branch"], cwd=repo_path, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo_path, check=True)

    repo = RepoConfig(
        repo_root=repo_path,
        use_worktree=True,
        worktree_base=tmp_path / "worktrees",
        default_branch="main",
    )
    task = prepare_task_workspace(
        repo,
        task_index=0,
        force_isolation=True,
        base_branch="feature/base",
    )
    assert (task.path / "base.txt").read_text() == "on base\n"
    task.teardown(keep=False)


def test_maybe_commit_recoverable_worktree(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)
    repo = RepoConfig(
        repo_root=repo_path,
        use_worktree=True,
        worktree_base=tmp_path / "worktrees",
    )
    task = prepare_task_workspace(repo, task_index=0, force_isolation=True)
    (task.path / "wip.txt").write_text("wip\n", encoding="utf-8")

    sha = maybe_commit_recoverable_worktree(
        task,
        "verify_failed",
        goal="fix tests",
    )
    assert sha is not None
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=task.path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert not status.stdout.strip()
    task.teardown(keep=False)


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


def test_commit_changes_retries_after_pre_commit_autofix(tmp_path: Path) -> None:
    """Pre-commit hooks (e.g. ruff format) often rewrite files and exit 1;
    commit_changes should re-stage and retry instead of crashing the run."""
    from agent_fleet.integrations.local_git import LocalGitOps

    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)

    hook = repo_path / ".git" / "hooks" / "pre-commit"
    counter = tmp_path / "hook_calls"
    hook.write_text(
        "#!/usr/bin/env bash\n"
        f"n=$(cat {counter} 2>/dev/null || echo 0)\n"
        f"echo $((n+1)) > {counter}\n"
        'if [ "$n" -eq 0 ]; then\n'
        "  printf 'rewritten\\n' > rewritten.txt\n"
        "  exit 1\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    (repo_path / "feature.txt").write_text("new feature\n", encoding="utf-8")

    ops = LocalGitOps(repo_path, use_worktree=False)
    sha = ops.commit_changes(repo_path, "test: autofix retry")
    assert sha is not None
    assert (repo_path / "rewritten.txt").read_text() == "rewritten\n"
    # Hook ran twice: once with autofix exit 1, then a clean pass.
    assert counter.read_text().strip() == "2"


def test_commit_changes_raises_when_hook_keeps_failing(tmp_path: Path) -> None:
    """If pre-commit fails without modifying anything, retry shouldn't paper over it."""
    from agent_fleet.integrations.local_git import LocalGitOps

    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)

    hook = repo_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/usr/bin/env bash\necho 'real failure' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    (repo_path / "feature.txt").write_text("new feature\n", encoding="utf-8")

    ops = LocalGitOps(repo_path, use_worktree=False)
    try:
        ops.commit_changes(repo_path, "test: real failure")
    except RuntimeError as exc:
        assert "git commit failed" in str(exc)
    else:
        raise AssertionError("expected RuntimeError when pre-commit fails without auto-fix")


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


def test_sweep_orphan_worktrees(tmp_path: Path) -> None:
    """Sweep removes worktrees whose branch is not in active_branches."""
    from agent_fleet.integrations.local_git import LocalGitOps
    from agent_fleet.pr_loop.worktree import release_worktree_lock, sweep_orphan_worktrees

    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)

    base = tmp_path / "worktrees"
    git_ops = LocalGitOps(repo_path, use_worktree=True, worktree_base=base)

    orphan_wt = git_ops.setup_workspace(repo_path, "orphan-run", "main", branch_name="fleet/orphan")
    active_wt = git_ops.setup_workspace(repo_path, "active-run", "main", branch_name="fleet/active")

    # setup_workspace claims a lock for the current PID; drop the orphan's
    # lock so the sweep treats it as eligible for removal.
    release_worktree_lock(orphan_wt)
    release_worktree_lock(active_wt)

    assert orphan_wt.exists()
    assert active_wt.exists()

    removed = sweep_orphan_worktrees(
        repo_path,
        base_path=base,
        active_branches={"fleet/active"},
    )

    assert removed == 1
    assert not orphan_wt.exists(), "orphan worktree should have been removed"
    assert active_wt.exists(), "active worktree should be kept"


def test_sweep_skips_worktree_locked_by_live_owner(tmp_path: Path) -> None:
    """A worktree with a sidecar lock pointing to a live PID survives the sweep."""
    from agent_fleet.integrations.local_git import LocalGitOps
    from agent_fleet.pr_loop.worktree import sweep_orphan_worktrees

    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)

    base = tmp_path / "worktrees"
    git_ops = LocalGitOps(repo_path, use_worktree=True, worktree_base=base)

    # setup_workspace auto-claims a lock for the current pytest PID.
    live_wt = git_ops.setup_workspace(repo_path, "live-run", "main", branch_name="fleet/live")
    assert (live_wt.parent / f"{live_wt.name}.lock").exists()

    removed = sweep_orphan_worktrees(
        repo_path,
        base_path=base,
        active_branches=set(),
    )

    assert removed == 0
    assert live_wt.exists(), "live-locked worktree must survive the sweep"


def test_sweep_removes_worktree_with_stale_pid_lock(tmp_path: Path) -> None:
    """A lock whose PID is dead (or recycled with different starttime) is stale."""
    from agent_fleet.integrations.local_git import LocalGitOps
    from agent_fleet.pr_loop.worktree import _worktree_lock_path, sweep_orphan_worktrees

    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)

    base = tmp_path / "worktrees"
    git_ops = LocalGitOps(repo_path, use_worktree=True, worktree_base=base)

    wt = git_ops.setup_workspace(repo_path, "stale-run", "main", branch_name="fleet/stale")
    # Overwrite the auto-claimed lock with a guaranteed-dead-or-mismatched record:
    # PID 1 (init) exists, but with an obviously bogus starttime so the
    # (pid, starttime) tuple does not validate.
    _worktree_lock_path(wt).write_text("1 999999999999\n")

    removed = sweep_orphan_worktrees(
        repo_path,
        base_path=base,
        active_branches=set(),
    )

    assert removed == 1
    assert not wt.exists(), "stale-lock worktree should be reclaimed"
