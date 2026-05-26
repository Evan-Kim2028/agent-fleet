"""Push fleet branches and open PRs after code_review completes."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from typing import TYPE_CHECKING

from agent_fleet.pr_loop import github_ops
from agent_fleet.repo import commit_preflight_commands_for_persona

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.repo import RepoConfig

logger = logging.getLogger(__name__)


def find_pr_for_branch(branch: str, *, cwd: Path) -> int | None:
    result = subprocess.run(
        ["gh", "pr", "list", "--head", branch, "--json", "number", "--limit", "1"],
        capture_output=True,
        text=True,
        cwd=cwd,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        items = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not items:
        return None
    return int(items[0]["number"])


def create_pr(
    *,
    branch: str,
    base: str,
    title: str,
    body: str,
    cwd: Path,
) -> int | None:
    result = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--head",
            branch,
            "--base",
            base,
            "--title",
            title,
            "--body",
            body,
        ],
        capture_output=True,
        text=True,
        cwd=cwd,
        check=False,
    )
    if result.returncode != 0:
        logger.warning("gh pr create failed: %s", result.stderr[:500])
        return find_pr_for_branch(branch, cwd=cwd)
    match = re.search(r"/pull/(\d+)", result.stdout or result.stderr or "")
    if match:
        return int(match.group(1))
    return find_pr_for_branch(branch, cwd=cwd)


def _rev_list_count(worktree: Path, range_spec: str) -> int | None:
    result = subprocess.run(
        ["git", "rev-list", "--count", range_spec],
        capture_output=True,
        text=True,
        cwd=worktree,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def commits_ahead_of_base(worktree: Path, branch: str, base: str) -> int:
    """Count commits on branch ahead of base (local HEAD or remote tracking ref)."""
    for tip in (f"origin/{branch}", branch, "HEAD"):
        for upstream in (f"origin/{base}", base):
            count = _rev_list_count(worktree, f"{upstream}..{tip}")
            if count is not None and count > 0:
                return count
    return 0


def push_branch_if_ahead(worktree: Path, branch: str) -> bool:
    """Push when HEAD is ahead of origin/branch (including already-committed work)."""
    ahead = _rev_list_count(worktree, f"origin/{branch}..HEAD")
    if ahead is not None and ahead <= 0:
        return False
    push = subprocess.run(
        ["git", "push", "-u", "origin", f"HEAD:{branch}"],
        capture_output=True,
        text=True,
        cwd=worktree,
        check=False,
        timeout=180,
    )
    if push.returncode != 0:
        logger.warning("push failed for %s: %s", branch, push.stderr[:300])
        return False
    return True


def publish_fleet_branch(
    *,
    worktree: Path,
    branch: str,
    repo: RepoConfig,
    task_goal: str,
    persona: str,
) -> int | None:
    """Commit, push, and return PR number (existing or newly created)."""
    message = f"feat(fleet): {task_goal[:72]}\n\n🤖 Agent: persona={persona}"
    push_result = github_ops.commit_and_push(
        worktree,
        message,
        branch,
        preflight_commands=commit_preflight_commands_for_persona(repo, persona),
    )
    if not push_result.ok:
        push_branch_if_ahead(worktree, branch)

    existing = find_pr_for_branch(branch, cwd=repo.repo_root)
    if existing:
        return existing

    if commits_ahead_of_base(worktree, branch, repo.default_branch) <= 0:
        logger.warning(
            "No commits on branch %s ahead of %s — skipping PR create",
            branch,
            repo.default_branch,
        )
        return None

    title = f"[Fleet/{persona}] {task_goal[:80]}"
    body = (
        f"Automated fleet dispatch.\n\n"
        f"**Persona:** `{persona}`\n\n"
        f"**Goal:** {task_goal}\n\n"
        f"🤖 Agent: persona={persona}"
    )
    logger.info("Opening PR for branch %s (base=%s)", branch, repo.default_branch)
    return create_pr(
        branch=branch,
        base=repo.default_branch,
        title=title,
        body=body,
        cwd=repo.repo_root,
    )
