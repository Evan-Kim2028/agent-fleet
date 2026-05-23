"""Push fleet branches and open PRs after code_review completes."""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING

from agent_fleet.pr_loop import github_ops

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
    import json

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
        existing = find_pr_for_branch(branch, cwd=cwd)
        return existing
    import re

    match = re.search(r"/pull/(\d+)", result.stdout or result.stderr or "")
    if match:
        return int(match.group(1))
    return find_pr_for_branch(branch, cwd=cwd)


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
    pushed = github_ops.commit_and_push(worktree, message, branch)
    if not pushed:
        existing = find_pr_for_branch(branch, cwd=repo.repo_root)
        if existing:
            return existing
        logger.warning("No changes to push for branch %s", branch)
        return None

    existing = find_pr_for_branch(branch, cwd=repo.repo_root)
    if existing:
        return existing

    title = f"[Fleet/{persona}] {task_goal[:80]}"
    body = (
        f"Automated fleet dispatch.\n\n"
        f"**Persona:** `{persona}`\n\n"
        f"**Goal:** {task_goal}\n\n"
        f"🤖 Agent: persona={persona}"
    )
    return create_pr(
        branch=branch,
        base=repo.default_branch,
        title=title,
        body=body,
        cwd=repo.repo_root,
    )
