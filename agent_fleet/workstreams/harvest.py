"""Merge fleet-run worktree branches onto feature branches."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 — used at runtime, not only in annotations


@dataclass(frozen=True)
class HarvestPlan:
    repo_root: Path
    worktree_path: Path
    source_sha: str
    source_branch: str
    target_branch: str
    base_branch: str


def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def plan_harvest(
    *,
    repo_root: Path,
    worktree_path: Path,
    target_branch: str,
    base_branch: str | None = None,
) -> HarvestPlan:
    """Validate inputs and return the merge plan without mutating git state."""
    repo_root = repo_root.resolve()
    worktree_path = worktree_path.resolve()
    if not worktree_path.is_dir():
        raise FileNotFoundError(f"Worktree not found: {worktree_path}")

    git_dir = _run_git(["rev-parse", "--git-dir"], cwd=worktree_path)
    if git_dir.returncode != 0:
        raise RuntimeError(f"Not a git worktree: {worktree_path}")

    head = _run_git(["rev-parse", "HEAD"], cwd=worktree_path)
    if head.returncode != 0:
        raise RuntimeError(f"Could not read worktree HEAD: {head.stderr.strip()}")
    source_sha = head.stdout.strip()

    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=worktree_path)
    source_branch = branch.stdout.strip() if branch.returncode == 0 else source_sha
    if source_branch == "HEAD":
        source_branch = source_sha

    base = base_branch or "main"
    return HarvestPlan(
        repo_root=repo_root,
        worktree_path=worktree_path,
        source_sha=source_sha,
        source_branch=source_branch,
        target_branch=target_branch,
        base_branch=base,
    )


def harvest_worktree(
    *,
    repo_root: Path,
    worktree_path: Path,
    target_branch: str,
    base_branch: str | None = None,
    dry_run: bool = False,
) -> str:
    """Merge the worktree HEAD onto target_branch. Returns merged commit sha."""
    plan = plan_harvest(
        repo_root=repo_root,
        worktree_path=worktree_path,
        target_branch=target_branch,
        base_branch=base_branch,
    )
    if dry_run:
        return plan.source_sha

    exists = _run_git(["rev-parse", "--verify", plan.target_branch], cwd=plan.repo_root)
    if exists.returncode != 0:
        base_ref = _run_git(["rev-parse", "--verify", plan.base_branch], cwd=plan.repo_root)
        start = base_ref.stdout.strip() if base_ref.returncode == 0 else "HEAD"
        created = _run_git(["branch", plan.target_branch, start], cwd=plan.repo_root)
        if created.returncode != 0:
            raise RuntimeError(f"Could not create {plan.target_branch}: {created.stderr.strip()}")

    checkout = _run_git(["checkout", plan.target_branch], cwd=plan.repo_root)
    if checkout.returncode != 0:
        raise RuntimeError(f"Could not checkout {plan.target_branch}: {checkout.stderr.strip()}")

    merge = _run_git(
        [
            "merge",
            "--no-ff",
            plan.source_sha,
            "-m",
            f"harvest: merge fleet work from {plan.source_branch}",
        ],
        cwd=plan.repo_root,
    )
    if merge.returncode != 0:
        _run_git(["merge", "--abort"], cwd=plan.repo_root)
        raise RuntimeError(
            f"Merge failed for {plan.target_branch} ← {plan.source_sha}: {merge.stderr.strip()}"
        )

    sha = _run_git(["rev-parse", "HEAD"], cwd=plan.repo_root)
    return sha.stdout.strip() if sha.returncode == 0 else plan.source_sha
