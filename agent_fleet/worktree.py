"""Git worktree isolation for parallel fleet dispatch."""

# ruff: noqa: TC001, TC003

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from agent_fleet.integrations.local_git import LocalGitOps
from agent_fleet.repo import RepoConfig
from agent_fleet.verify_core import is_git_repo


@dataclass(frozen=True)
class TaskWorkspace:
    """Effective workspace for a fleet task run."""

    path: Path
    repo_root: Path
    isolated: bool
    branch_name: str | None = None
    run_id: str | None = None
    git_ops: LocalGitOps | None = None

    def teardown(self, *, keep: bool = False) -> None:
        if not self.isolated or self.git_ops is None or keep:
            return
        self.git_ops.teardown_workspace(self.path)


def should_isolate_worktree(
    repo: RepoConfig | None,
    *,
    batch_size: int,
    same_workspace_tasks: int,
) -> bool:
    """Return True when the task should run in an isolated git worktree."""
    if repo is not None and repo.use_worktree:
        return True
    return batch_size > 1 and same_workspace_tasks > 1


def prepare_task_workspace(
    repo: RepoConfig,
    *,
    task_index: int,
    force_isolation: bool = False,
) -> TaskWorkspace:
    """Create an isolated worktree for a fleet task, or return the repo root."""
    repo_root = repo.repo_root.resolve()
    if not force_isolation and not repo.use_worktree:
        return TaskWorkspace(
            path=repo_root,
            repo_root=repo_root,
            isolated=False,
        )

    if not is_git_repo(repo_root):
        raise RuntimeError(
            f"Worktree isolation requires a git repo at {repo_root}. "
            "Initialize git or disable parallel dispatch for this path."
        )

    run_id = f"task-{task_index}-{uuid.uuid4().hex[:8]}"
    branch_name = f"fleet/task-{task_index}-{run_id.split('-')[-1]}"
    git_ops = LocalGitOps(
        repo_root,
        use_worktree=True,
        worktree_base=repo.worktree_base,
    )
    worktree = git_ops.setup_workspace(
        repo_root,
        run_id,
        repo.default_branch,
        branch_name=branch_name,
    )
    return TaskWorkspace(
        path=worktree,
        repo_root=repo_root,
        isolated=True,
        branch_name=branch_name,
        run_id=run_id,
        git_ops=git_ops,
    )
