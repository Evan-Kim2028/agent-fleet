"""Resolve agent worktree paths across legacy and fleet naming conventions."""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

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


def _parse_worktree_list(stdout: str) -> list[dict[str, str]]:
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
            if ref.startswith("refs/heads/"):
                current["branch"] = ref[len("refs/heads/") :]
            else:
                current["branch"] = ref
    if current:
        entries.append(current)
    return entries


def _worktree_in_use(worktree_path: Path) -> bool:
    """True if any process has *worktree_path* (or a descendant) as its cwd.

    Walks /proc/*/cwd. Used to avoid deleting a worktree out from under a
    live dispatcher whose branch hasn't yet become an open PR.
    """
    try:
        target = worktree_path.resolve()
    except OSError:
        return False
    proc = Path("/proc")
    if not proc.is_dir():
        return False
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cwd = (entry / "cwd").resolve()
        except (OSError, PermissionError):
            continue
        try:
            cwd.relative_to(target)
        except ValueError:
            continue
        return True
    return False


def remove_worktree(repo_root: Path, worktree_path: Path) -> bool:
    """Force-remove *worktree_path* from the git worktree registry. Returns True on success."""
    if _worktree_in_use(worktree_path):
        logger.info("Skipping worktree removal — in use: %s", worktree_path)
        return False
    rm = subprocess.run(
        ["git", "-C", str(repo_root), "worktree", "remove", "--force", str(worktree_path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if rm.returncode == 0:
        logger.info("Removed worktree %s", worktree_path)
        return True
    logger.warning("Failed to remove worktree %s: %s", worktree_path, rm.stderr.strip())
    return False


def sweep_orphan_worktrees(
    repo_root: Path,
    base_path: Path,
    active_branches: set[str],
) -> int:
    """Remove worktrees under *base_path* whose branch is not in *active_branches*.

    Used by the PR loop watcher to clean up after merged/closed PRs without
    relying on directory mtime.
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

    removed = 0
    for entry in _parse_worktree_list(result.stdout):
        wt_path = Path(entry.get("worktree", ""))
        branch = entry.get("branch", "")
        try:
            wt_path.relative_to(base_path)
        except ValueError:
            continue
        if not branch or branch in active_branches:
            continue
        if remove_worktree(repo_root, wt_path):
            removed += 1
    return removed


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
        if candidate.exists() and ((candidate / ".git").exists() or (candidate / ".git").is_file()):
            return candidate
    meta = parse_agent_branch(branch)
    if meta:
        return worktree_base / meta.run_id
    return worktree_candidates(branch, worktree_base)[-1]
