"""Resolve agent worktree paths across legacy and fleet naming conventions."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_BRANCH_RE = re.compile(
    r"^(?:fleet|agent)/(?P<persona>[^/]+)/(?P<issue>\d+)-(?P<run_id>[0-9a-f]+)$"
)


@dataclass(frozen=True)
class BranchRunMeta:
    prefix: str
    persona: str
    issue: str
    run_id: str


def parse_agent_branch(branch: str) -> BranchRunMeta | None:
    m = _BRANCH_RE.match(branch)
    if not m:
        return None
    return BranchRunMeta(
        prefix=branch.split("/", 1)[0],
        persona=m["persona"],
        issue=m["issue"],
        run_id=m["run_id"],
    )


def worktree_candidates(branch: str, base: Path) -> list[Path]:
    """All known worktree dir layouts, most-specific first."""
    meta = parse_agent_branch(branch)
    candidates: list[Path] = []
    if meta:
        # Legacy Kimi dispatch: 1532-data-0837d5d0
        candidates.append(base / f"{meta.issue}-{meta.persona}-{meta.run_id}")
        # Fleet runner / agent_fleet issue dispatch: 0837d5d0
        candidates.append(base / meta.run_id)
    # Current pr_loop fallback (keep during transition)
    candidates.append(base / re.sub(r"[^\w.-]+", "_", branch))
    seen: set[Path] = set()
    out: list[Path] = []
    for path in candidates:
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def registered_worktree_for_branch(repo_root: Path, branch: str) -> Path | None:
    """Resolve via ``git worktree list --porcelain`` (ground truth)."""
    ref_suffix = f"refs/heads/{branch}"
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    current_path: Path | None = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = Path(line.split(" ", 1)[1])
        elif line.startswith("branch ") and line.endswith(ref_suffix) and current_path:
            return current_path
    return None


def resolve_worktree_path(
    branch: str,
    *,
    repo_root: Path,
    worktree_base: Path,
) -> Path:
    """Pick existing worktree or preferred path for creation."""
    registered = registered_worktree_for_branch(repo_root, branch)
    if registered is not None:
        return registered
    for candidate in worktree_candidates(branch, worktree_base):
        if candidate.exists() and (
            (candidate / ".git").exists() or (candidate / ".git").is_file()
        ):
            return candidate
    meta = parse_agent_branch(branch)
    if meta:
        return worktree_base / meta.run_id
    return worktree_candidates(branch, worktree_base)[-1]
