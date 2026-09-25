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


def _git_sha(cwd: str, ref: str) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=5,
    )
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else None


def _merge_base(cwd: str, ref: str) -> str | None:
    result = subprocess.run(
        ["git", "merge-base", ref, "HEAD"],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=5,
    )
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else None


def _diff_base(cwd: str) -> str | None:
    """Best-effort base for the task's diff, resolved from local refs only.

    Prefer merge-base against a base branch (origin/<default> if the ref already
    exists locally, else local main/master that isn't the current branch); fall
    back to HEAD's parent. Unlike verify_core._resolve_diff_base this never
    fetches: changed_lines runs in the run_end hot path and an untimeouted
    network op could stall the runner. None when nothing resolves (e.g. a repo
    whose only commit is the root commit).
    """
    current = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=5,
    ).stdout.strip()
    origin_head_result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=5,
    )
    # A missing origin/HEAD still echoes "origin/HEAD" on stdout (rc 128), so
    # the returncode -- not the text -- tells us whether the ref exists.
    origin_head = origin_head_result.stdout.strip() if origin_head_result.returncode == 0 else ""
    default = origin_head.split("/")[-1] if origin_head else "main"
    candidates: list[str] = []
    if origin_head:
        candidates.append(origin_head)
    for name in (default, "main", "master"):
        if name != current and name not in candidates:
            candidates.append(name)
    for ref in candidates:
        if _git_sha(cwd, ref) is None:
            continue
        base = _merge_base(cwd, ref)
        if base is not None:
            return base
    return _git_sha(cwd, "HEAD^")


def changed_lines(workspace: Path | str | None) -> int:
    """Total changed lines (additions + deletions) for the task's work in *workspace*.

    The task's contribution is measured as one diff of the working tree against
    the resolved base (see _diff_base): committed work on the branch plus
    uncommitted tracked edits, plus untracked-file lines counted separately.
    A stray dirty file can no longer mask committed work. Returns 0 on any
    error (not a git repo, no commits, OSError, timeout).
    """
    if workspace is None:
        return 0
    ws = Path(workspace)
    if not ws.is_dir():
        return 0
    cwd = str(ws)
    try:
        base = _diff_base(cwd) or "HEAD"
        return _numstat_total([base], cwd) + _untracked_lines(cwd, ws)
    except OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError:
        return 0
