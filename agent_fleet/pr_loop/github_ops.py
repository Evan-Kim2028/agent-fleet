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
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr
        )
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
        "number,headRefName,labels,isDraft,mergeable,mergeStateStatus",
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
    filtered = [
        check
        for check in checks
        if str(check.get("name", "")).lower() not in ignored_set
    ]
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


def add_pr_label(pr_number: int, label: str, *, cwd: Path | None = None) -> None:
    _gh("label", "create", label, "--force", cwd=cwd, check=False)
    _gh("pr", "edit", str(pr_number), "--add-label", label, cwd=cwd, check=False)


def merge_pr(
    pr_number: int,
    *,
    subject: str,
    body: str,
    cwd: Path | None = None,
) -> bool:
    result = _gh(
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
    if result.returncode == 0:
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


def checkout_branch(branch: str, worktree: Path, *, repo_root: Path) -> None:
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if worktree.exists() and (
        (worktree / ".git").exists() or (worktree / ".git").is_file()
    ):
        subprocess.run(["git", "fetch", "origin", branch], cwd=worktree, check=True, timeout=120)
        subprocess.run(["git", "checkout", branch], cwd=worktree, check=False, timeout=60)
        subprocess.run(
            ["git", "reset", "--hard", f"origin/{branch}"],
            cwd=worktree,
            check=True,
            timeout=60,
        )
        return

    listed = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if listed.returncode == 0:
        for line in listed.stdout.splitlines():
            if line.startswith("worktree ") and line.endswith(f"/{branch}"):
                return
            if line.startswith("branch ") and line.endswith(f"/{branch}"):
                return

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
                subprocess.run(["git", "fetch", "origin", branch], cwd=path, check=True, timeout=120)
                subprocess.run(
                    ["git", "reset", "--hard", f"origin/{branch}"],
                    cwd=path,
                    check=True,
                    timeout=60,
                )
                return
        add.check_returncode()


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
    subprocess.run(["git", "add", "--"] + changed, cwd=worktree, check=True, timeout=60)
    commit = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if commit.returncode != 0:
        logger.warning("commit failed: %s", commit.stderr[:300])
        return False
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
