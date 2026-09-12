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


_RECOVERABLE_WORKTREE_STATUSES = frozenset({"review_changes_requested", "verify_failed"})


def should_keep_task_worktree(
    status: str,
    *,
    auto_push: bool = False,
    isolated: bool = False,
    has_changes: bool = False,
) -> bool:
    """Return True when an isolated worktree should survive teardown."""
    if status in {"completed", "merged"}:
        return True
    if auto_push and isolated:
        return True
    return status in _RECOVERABLE_WORKTREE_STATUSES and has_changes


def maybe_commit_recoverable_worktree(
    task_workspace: TaskWorkspace,
    status: str,
    *,
    goal: str,
) -> str | None:
    """Commit uncommitted changes before teardown on recoverable soft failures."""
    if (
        status not in _RECOVERABLE_WORKTREE_STATUSES
        or not task_workspace.isolated
        or task_workspace.git_ops is None
    ):
        return None
    message = f"fleet: auto-commit after {status}\n\n{goal.strip()[:500]}"
    return task_workspace.git_ops.commit_changes(task_workspace.path, message)


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
    base_branch: str | None = None,
    resume: bool = True,
) -> TaskWorkspace:
    """Create an isolated worktree for a fleet task, or return the repo root.

    When ``resume`` (default True, matching ``FleetRunConfig.resume``'s
    default), first looks for an existing ``fleet/task-{task_index}-*``
    branch with local changes — e.g. left behind by a fleet run process that
    was hard-killed (SIGTERM) mid-task — and reuses that worktree/branch
    instead of creating a fresh one. Without this, every dispatch of "the
    same" task_index (a DAG task redispatched after an interruption) got a
    brand-new worktree, branch, and (for session-capable backends like Devin)
    a brand-new agent session with no memory of the killed attempt's
    progress — the dispatcher path had no connection at all to the
    ResumableGitOps resume mechanism LocalFleetRunner uses for its "full"
    pipeline. A backend session id captured against the resumed worktree path
    is picked up automatically by session_store.load_session_id — see
    dispatcher_task.run_configured_pipeline.
    """
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

    git_ops = LocalGitOps(
        repo_root,
        use_worktree=True,
        worktree_base=repo.worktree_base,
    )

    if resume:
        resumed = git_ops.find_resume_branch_by_prefix(f"fleet/task-{task_index}-")
        if resumed is not None:
            branch_name, run_id = resumed
            worktree = git_ops.attach_worktree(branch_name, run_id, create=False)
            if worktree is not None:
                return TaskWorkspace(
                    path=worktree,
                    repo_root=repo_root,
                    isolated=True,
                    branch_name=branch_name,
                    run_id=run_id,
                    git_ops=git_ops,
                )

    run_id = f"task-{task_index}-{uuid.uuid4().hex[:8]}"
    branch_name = f"fleet/task-{task_index}-{run_id.split('-')[-1]}"
    worktree = git_ops.setup_workspace(
        repo_root,
        run_id,
        base_branch or repo.default_branch,
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
