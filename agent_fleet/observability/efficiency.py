"""Shared efficiency helpers for fleet observability."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _numstat_total(diff_args: list[str], cwd: str) -> int:
    result = subprocess.run(
        ["git", "diff", "--numstat", *diff_args],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=5,
    )
    if result.returncode != 0:
        return 0
    total = 0
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        try:
            total += int(parts[0]) + int(parts[1])
        except ValueError:
            continue  # binary file: numstat reports "-\t-"
    return total


def _untracked_lines(cwd: str, ws: Path) -> int:
    result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=5,
    )
    if result.returncode != 0:
        return 0
    total = 0
    for rel in result.stdout.splitlines():
        rel = rel.strip()
        if not rel:
            continue
        try:
            with (ws / rel).open("rb") as fh:
                total += sum(1 for _ in fh)
        except OSError:
            continue
    return total


def changed_lines(workspace: Path | str | None) -> int:
    """Total changed lines (additions + deletions) for the task's work in *workspace*.

    The work may be uncommitted (dispatcher path, and the runner's pre-commit
    early exits) or already committed (the runner success path commits before
    run_end). Measure the working-tree delta vs HEAD plus untracked-file lines
    first; when the tree is clean the work is committed, so fall back to the last
    commit (HEAD~1..HEAD). Returns 0 on any error (not a git repo, no commits,
    only one commit so HEAD~1 doesn't exist, OSError, timeout).
    """
    if workspace is None:
        return 0
    ws = Path(workspace)
    if not ws.is_dir():
        return 0
    cwd = str(ws)
    try:
        working = _numstat_total(["HEAD"], cwd) + _untracked_lines(cwd, ws)
        if working > 0:
            return working
        return _numstat_total(["HEAD~1..HEAD"], cwd)
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return 0
