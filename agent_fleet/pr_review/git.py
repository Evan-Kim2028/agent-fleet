"""Git diff helpers for PR analysis."""

from __future__ import annotations

import re
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def get_merge_base(base_ref: str, head_ref: str, *, cwd: Path) -> str:
    result = subprocess.run(
        ["git", "merge-base", base_ref, head_ref],
        capture_output=True,
        text=True,
        cwd=cwd,
        check=True,
    )
    return result.stdout.strip()


def get_diff(base_ref: str, head_ref: str, *, cwd: Path) -> str:
    merge_base = get_merge_base(base_ref, head_ref, cwd=cwd)
    result = subprocess.run(
        ["git", "diff", merge_base, head_ref],
        capture_output=True,
        text=True,
        cwd=cwd,
        check=True,
    )
    return result.stdout


def get_changed_files(base_ref: str, head_ref: str, *, cwd: Path) -> list[str]:
    merge_base = get_merge_base(base_ref, head_ref, cwd=cwd)
    result = subprocess.run(
        ["git", "diff", "--name-only", merge_base, head_ref],
        capture_output=True,
        text=True,
        cwd=cwd,
        check=True,
    )
    return [line for line in result.stdout.strip().split("\n") if line]


def get_working_tree_diff(*, cwd: Path, base_branch: str = "main") -> tuple[str, list[str]]:
    """Return merge-base diff and changed files for HEAD vs origin/base_branch."""
    subprocess.run(
        ["git", "fetch", "origin", base_branch],
        cwd=cwd,
        capture_output=True,
        check=False,
    )
    head = "HEAD"
    base = f"origin/{base_branch}"
    merge_base_result = subprocess.run(
        ["git", "merge-base", base, head],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    diff_target = merge_base_result.stdout.strip() or base
    diff_result = subprocess.run(
        ["git", "diff", diff_target, head],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    files_result = subprocess.run(
        ["git", "diff", "--name-only", diff_target, head],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    files = [line for line in files_result.stdout.strip().split("\n") if line]
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    for path in untracked.stdout.strip().split("\n"):
        if path:
            files.append(path)
    return diff_result.stdout, sorted(set(files))


def diff_for_files(full_diff: str, files: list[str]) -> str:
    if not files:
        return ""
    pattern = re.compile(r"^diff --git a/(?:" + "|".join(re.escape(f) for f in files) + r")")
    lines = full_diff.splitlines(keepends=True)
    result: list[str] = []
    in_hunk = False
    for line in lines:
        if line.startswith("diff --git"):
            in_hunk = bool(pattern.match(line))
        if in_hunk:
            result.append(line)
    return "".join(result)


def truncate_diff(diff: str, max_chars: int) -> str:
    if len(diff) <= max_chars:
        return diff
    return diff[:max_chars] + f"\n\n... [diff truncated: {len(diff)} chars total]"


def is_trivial_pr(files: list[str], patterns: tuple[str, ...]) -> bool:
    if not files:
        return True
    return all(any(re.search(pattern, path) for pattern in patterns) for path in files)


def is_oversized_pr(files: list[str], threshold: int) -> bool:
    return len(files) > threshold


def is_deletion_only_pr(diff: str) -> bool:
    added = 0
    removed = 0
    for line in diff.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return removed > 0 and added == 0


def classify_files(
    files: list[str],
    area_prefixes: dict[str, tuple[str, ...]],
) -> dict[str, list[str]]:
    frontend_prefixes = area_prefixes.get("frontend", ())
    backend_prefixes = area_prefixes.get("backend", ())
    frontend: list[str] = []
    backend: list[str] = []
    other: list[str] = []
    for path in files:
        if any(path.startswith(prefix) for prefix in frontend_prefixes):
            frontend.append(path)
        elif any(path.startswith(prefix) for prefix in backend_prefixes):
            backend.append(path)
        else:
            other.append(path)
    return {"frontend": frontend, "backend": backend, "other": other}
