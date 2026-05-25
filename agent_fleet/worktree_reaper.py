"""Reap stale git worktrees that are no longer associated with active branches."""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _parse_worktree_list(stdout: str) -> list[dict[str, str]]:
    """Parse ``git worktree list --porcelain`` output into dicts.

    Each entry contains at least ``worktree`` (path) and optionally ``branch``
    (the short branch name without the ``refs/heads/`` prefix).
    """
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                entries.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            current["worktree"] = line[len("worktree ") :]
        elif line.startswith("branch "):
            ref = line[len("branch ") :]
            # refs/heads/<name> → <name>
            if ref.startswith("refs/heads/"):
                current["branch"] = ref[len("refs/heads/") :]
            else:
                current["branch"] = ref
    if current:
        entries.append(current)
    return entries


def reap_stale_worktrees(
    repo_root: Path,
    base_path: Path,
    max_age_s: int,
    active_branches: set[str],
) -> int:
    """Remove stale worktrees under *base_path* that are no longer active.

    A worktree is considered stale when ALL of the following are true:
    - Its path is under *base_path*.
    - Its branch is not in *active_branches*.
    - Its directory mtime is older than *max_age_s* seconds.

    Returns the count of worktrees removed.
    """
    result = subprocess.run(
        ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        logger.warning(
            "git worktree list failed (rc=%s): %s",
            result.returncode,
            result.stderr.strip(),
        )
        return 0

    entries = _parse_worktree_list(result.stdout)
    now = time.time()
    removed = 0

    for entry in entries:
        wt_path = Path(entry.get("worktree", ""))
        branch = entry.get("branch", "")

        # Only operate on worktrees under base_path.
        try:
            wt_path.relative_to(base_path)
        except ValueError:
            continue

        # Skip if branch is still active.
        if branch in active_branches:
            continue

        # Skip if directory is too fresh.
        try:
            age_s = now - wt_path.stat().st_mtime
        except OSError:
            # Directory already gone — nothing to clean up.
            continue
        if age_s < max_age_s:
            continue

        logger.info(
            "Reaping stale worktree %s (branch=%r, age=%.0fs)",
            wt_path,
            branch or "(detached)",
            age_s,
        )
        rm = subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "remove", "--force", str(wt_path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if rm.returncode == 0:
            removed += 1
        else:
            logger.warning(
                "Failed to remove worktree %s: %s",
                wt_path,
                rm.stderr.strip(),
            )

    return removed
