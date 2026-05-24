"""GitHub operations for PR loop (via gh CLI)."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _gh(
    *args: str,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    cmd = ["gh", *args]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        check=False,
        timeout=120,
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return result


def list_open_fleet_prs(
    *,
    branch_prefixes: tuple[str, ...],
    cwd: Path | None = None,
) -> list[dict[str, Any]]:
    result = _gh(
        "pr",
        "list",
        "--state",
        "open",
        "--json",
        "number,headRefName,labels,isDraft,mergeable,mergeStateStatus,createdAt",
        "--limit",
        "50",
        cwd=cwd,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        prs = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    return [
        pr
        for pr in prs
        if any(str(pr.get("headRefName", "")).startswith(prefix) for prefix in branch_prefixes)
    ]


def pr_comments(pr_number: int, *, cwd: Path | None = None) -> list[dict[str, Any]]:
    result = _gh("pr", "view", str(pr_number), "--json", "comments", cwd=cwd, check=False)
    if result.returncode != 0:
        return []
    return json.loads(result.stdout).get("comments", [])


def pr_checks(
    pr_number: int,
    *,
    cwd: Path | None = None,
    ignored: tuple[str, ...] = (),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (all_checks, pending, failed) excluding ignored check names."""
    result = _gh(
        "pr",
        "checks",
        str(pr_number),
        "--json",
        "name,state,bucket",
        cwd=cwd,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return [], [], []
    try:
        checks = json.loads(result.stdout)
    except json.JSONDecodeError:
        return [], [], []

    ignored_set = {name.lower() for name in ignored}
    filtered = [check for check in checks if str(check.get("name", "")).lower() not in ignored_set]
    pending = [c for c in filtered if c.get("bucket") == "pending"]
    failed = [c for c in filtered if c.get("bucket") == "fail"]
    return filtered, pending, failed


def pr_changed_files(pr_number: int, *, cwd: Path | None = None) -> list[str]:
    result = _gh("pr", "view", str(pr_number), "--json", "files", cwd=cwd, check=False)
    if result.returncode != 0:
        return []
    files = json.loads(result.stdout).get("files", [])
    return [str(item.get("path", "")) for item in files if item.get("path")]


def pr_diff(pr_number: int, *, cwd: Path | None = None) -> str:
    result = _gh("pr", "diff", str(pr_number), cwd=cwd, check=False)
    if result.returncode != 0:
        return ""
    return result.stdout


def pr_has_label(pr_number: int, label: str, *, cwd: Path | None = None) -> bool:
    result = _gh("pr", "view", str(pr_number), "--json", "labels", cwd=cwd, check=False)
    if result.returncode != 0:
        return False
    labels = json.loads(result.stdout).get("labels", [])
    return any(str(item.get("name", "")) == label for item in labels)


def pr_has_blocking_review(pr_number: int, *, cwd: Path | None = None) -> bool:
    result = _gh(
        "pr",
        "view",
        str(pr_number),
        "--json",
        "reviewDecision,reviews",
        cwd=cwd,
        check=False,
    )
    if result.returncode != 0:
        return False
    payload = json.loads(result.stdout)
    if payload.get("reviewDecision") == "CHANGES_REQUESTED":
        return True
    for review in payload.get("reviews") or []:
        if review.get("state") == "CHANGES_REQUESTED":
            return True
    return False


def post_pr_comment(body: str, pr_number: int, *, cwd: Path | None = None) -> None:
    _gh("pr", "comment", str(pr_number), "--body", body, cwd=cwd)


def create_issue(
    *,
    title: str,
    body: str,
    labels: list[str] | None = None,
    cwd: Path | None = None,
) -> int | None:
    """Create a GitHub issue. Returns the issue number, or None on failure."""
    cmd = ["issue", "create", "--title", title, "--body", body]
    for label in labels or []:
        cmd.extend(["--label", label])
    result = _gh(*cmd, cwd=cwd, check=False)
    if result.returncode != 0:
        logger.warning("create_issue failed: %s", (result.stderr or "").strip()[:300])
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if "/issues/" in line:
            try:
                return int(line.rsplit("/", 1)[1])
            except ValueError:
                continue
    return None


def add_pr_label(pr_number: int, label: str, *, cwd: Path | None = None) -> None:
    _gh("label", "create", label, "--force", cwd=cwd, check=False)
    _gh("pr", "edit", str(pr_number), "--add-label", label, cwd=cwd, check=False)


def pr_is_draft(pr_number: int, *, cwd: Path | None = None) -> bool:
    result = _gh("pr", "view", str(pr_number), "--json", "isDraft", cwd=cwd, check=False)
    if result.returncode != 0:
        return False
    return bool(json.loads(result.stdout).get("isDraft"))


def _attempt_squash_merge(
    pr_number: int,
    *,
    subject: str,
    body: str,
    cwd: Path | None,
) -> subprocess.CompletedProcess[str]:
    return _gh(
        "pr",
        "merge",
        str(pr_number),
        "--squash",
        "--subject",
        subject,
        "--body",
        body,
        cwd=cwd,
        check=False,
    )


def _pr_is_behind_base(pr_number: int, *, cwd: Path | None) -> bool:
    """Detect 'PR branch is behind main' state via mergeStateStatus."""
    result = _gh(
        "pr",
        "view",
        str(pr_number),
        "--json",
        "mergeStateStatus",
        cwd=cwd,
        check=False,
    )
    if result.returncode != 0:
        return False
    status = json.loads(result.stdout).get("mergeStateStatus", "")
    return status == "BEHIND"


def update_branch(pr_number: int, *, cwd: Path | None = None) -> bool:
    """Server-side merge of base into the PR branch via `gh pr update-branch`.

    Returns True if GitHub accepted the request (branch was updated or already
    up to date). Returns False if there is a real merge conflict — only a human
    or the implementer agent can resolve that.
    """
    result = _gh("pr", "update-branch", str(pr_number), cwd=cwd, check=False)
    if result.returncode == 0:
        return True
    logger.warning("update-branch failed for PR #%s: %s", pr_number, result.stderr[:300])
    return False


def merge_pr(
    pr_number: int,
    *,
    subject: str,
    body: str,
    cwd: Path | None = None,
) -> bool:
    if pr_is_draft(pr_number, cwd=cwd):
        mark_pr_ready(pr_number, cwd=cwd)
    result = _attempt_squash_merge(pr_number, subject=subject, body=body, cwd=cwd)
    if result.returncode == 0:
        return True
    # Common mechanical failure: PR branch is behind main. Ask GitHub to merge
    # main into the PR branch server-side, then retry the squash merge.
    if _pr_is_behind_base(pr_number, cwd=cwd) and update_branch(pr_number, cwd=cwd):
        # update-branch triggers a new CI run; give it a moment and retry once.
        time.sleep(10)
        retry = _attempt_squash_merge(pr_number, subject=subject, body=body, cwd=cwd)
        if retry.returncode == 0:
            return True
    for _ in range(19):
        time.sleep(5)
        state_result = _gh("pr", "view", str(pr_number), "--json", "state", cwd=cwd, check=False)
        if state_result.returncode == 0:
            state = json.loads(state_result.stdout).get("state", "")
            if state == "MERGED":
                return True
    logger.warning("merge failed for PR #%s: %s", pr_number, result.stderr[:300])
    return False


def mark_pr_ready(pr_number: int, *, cwd: Path | None = None) -> None:
    _gh("pr", "ready", str(pr_number), cwd=cwd, check=False)


def checkout_branch(branch: str, worktree: Path, *, repo_root: Path) -> Path:
    from agent_fleet.pr_loop.worktree import (
        registered_worktree_for_branch,
        resolve_worktree_path,
    )

    registered = registered_worktree_for_branch(repo_root, branch)
    if registered is not None:
        worktree = registered
    elif not (
        worktree.exists() and ((worktree / ".git").exists() or (worktree / ".git").is_file())
    ):
        worktree = resolve_worktree_path(
            branch,
            repo_root=repo_root,
            worktree_base=worktree.parent,
        )

    worktree.parent.mkdir(parents=True, exist_ok=True)
    if worktree.exists() and ((worktree / ".git").exists() or (worktree / ".git").is_file()):
        subprocess.run(["git", "fetch", "origin", branch], cwd=worktree, check=True, timeout=120)
        subprocess.run(["git", "checkout", branch], cwd=worktree, check=False, timeout=60)
        subprocess.run(
            ["git", "reset", "--hard", f"origin/{branch}"],
            cwd=worktree,
            check=True,
            timeout=60,
        )
        return worktree

    subprocess.run(
        ["git", "fetch", "origin", branch],
        cwd=repo_root,
        check=True,
        timeout=120,
    )
    add = subprocess.run(
        ["git", "worktree", "add", "-B", branch, str(worktree), f"origin/{branch}"],
        cwd=repo_root,
        check=False,
        timeout=120,
    )
    if add.returncode != 0:
        existing = subprocess.run(
            ["git", "worktree", "list"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        for row in existing.stdout.splitlines():
            if branch in row:
                path = row.split()[0]
                subprocess.run(
                    ["git", "fetch", "origin", branch],
                    cwd=path,
                    check=True,
                    timeout=120,
                )
                subprocess.run(
                    ["git", "reset", "--hard", f"origin/{branch}"],
                    cwd=path,
                    check=True,
                    timeout=60,
                )
                return Path(path)
        add.check_returncode()
    return worktree


def commit_and_push(
    worktree: Path,
    message: str,
    branch: str,
    *,
    exclude: tuple[str, ...] = (),
) -> bool:
    exclude_set = set(exclude)
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        cwd=worktree,
        check=False,
        timeout=30,
    )
    changed = [
        line[3:].strip()
        for line in status.stdout.splitlines()
        if line.strip() and len(line) > 3 and line[3:].strip() not in exclude_set
    ]
    if not changed:
        return False
    max_hook_retries = 2
    for attempt in range(max_hook_retries + 1):
        # Re-add: a previous attempt's pre-commit hook may have auto-formatted
        # files (ruff format, eol-fixer); re-staging picks those up.
        subprocess.run(["git", "add", "-A"], cwd=worktree, check=True, timeout=60)
        commit = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if commit.returncode == 0:
            break
        post_status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=worktree,
            check=False,
            timeout=30,
        )
        if not post_status.stdout.strip() or attempt == max_hook_retries:
            logger.warning("commit failed: %s", commit.stderr[:300])
            return False
        logger.info("commit attempt %d failed (hook autofix?), retrying", attempt + 1)
    push = subprocess.run(
        ["git", "push", "origin", f"HEAD:{branch}"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    if push.returncode != 0:
        logger.warning("push failed: %s", push.stderr[:300])
        return False
    return True
