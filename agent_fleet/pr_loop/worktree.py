"""Resolve agent worktree paths across legacy and fleet naming conventions."""

from __future__ import annotations

import contextlib
import logging
import os
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

    Walks /proc/*/cwd. Coarse heuristic — misses processes that take the
    worktree as an argument but run from elsewhere (the common case for
    cursor-agent and ``git -C <path>`` calls). Kept as defense-in-depth
    alongside the precise PID-lock check in ``_worktree_locked``.
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
        except OSError, PermissionError:
            continue
        try:
            cwd.relative_to(target)
        except ValueError:
            continue
        return True
    return False


def _alive_dispatch_issue_numbers() -> set[int]:
    """ISSUE_NUMBER env values of currently-alive dispatcher subprocesses.

    The watcher spawns each issue dispatch as a subprocess with
    ``ISSUE_NUMBER`` in its environ. While that subprocess is alive its
    worktree must not be swept, regardless of whether the branch has yet
    surfaced as an open PR.
    """
    out: set[int] = set()
    proc = Path("/proc")
    if not proc.is_dir():
        return out
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            data = (entry / "environ").read_bytes()
        except OSError, PermissionError:
            continue
        for chunk in data.split(b"\x00"):
            if chunk.startswith(b"ISSUE_NUMBER="):
                with contextlib.suppress(ValueError):
                    out.add(int(chunk[len(b"ISSUE_NUMBER=") :]))
                break
    return out


def _worktree_lock_path(worktree_path: Path) -> Path:
    """Sidecar lock file path for *worktree_path*. Lives next to the worktree dir."""
    return worktree_path.parent / f"{worktree_path.name}.lock"


def _proc_start_time(pid: int) -> int | None:
    """Read field 22 (starttime, clock ticks since boot) from /proc/<pid>/stat."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError, PermissionError:
        return None
    # Field 2 is comm in parens, which may contain spaces. Split after the
    # closing paren so subsequent fields are positional.
    rparen = stat.rfind(")")
    if rparen < 0:
        return None
    rest = stat[rparen + 2 :].split()
    # rest[0] is field 3 (state); starttime is field 22, so index 19 here.
    try:
        return int(rest[19])
    except IndexError, ValueError:
        return None


def claim_worktree_lock(worktree_path: Path, pid: int | None = None) -> None:
    """Write a sidecar lock declaring *pid* (default: caller) owns *worktree_path*.

    Format: ``<pid> <starttime_clock_ticks>``. The starttime pins a specific
    process incarnation so PID reuse cannot trick the sweeper into preserving
    an orphaned worktree.
    """
    owner = pid if pid is not None else os.getpid()
    start = _proc_start_time(owner)
    if start is None:
        return
    lock = _worktree_lock_path(worktree_path)
    with contextlib.suppress(OSError):
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(f"{owner} {start}\n")


def release_worktree_lock(worktree_path: Path) -> None:
    """Remove the sidecar lock for *worktree_path* if present."""
    with contextlib.suppress(OSError):
        _worktree_lock_path(worktree_path).unlink()


def _worktree_locked(worktree_path: Path) -> bool:
    """True if a live process declared ownership of *worktree_path* via lock file.

    The lock binds (pid, starttime) — a recycled PID with a different starttime
    is treated as stale, so the sweeper can reclaim worktrees abandoned by
    crashed dispatchers without waiting for a watcher restart.
    """
    lock = _worktree_lock_path(worktree_path)
    try:
        text = lock.read_text().strip()
    except OSError, ValueError:
        return False
    parts = text.split()
    if len(parts) < 2:
        return False
    try:
        pid = int(parts[0])
        recorded_start = int(parts[1])
    except ValueError:
        return False
    actual_start = _proc_start_time(pid)
    return actual_start is not None and actual_start == recorded_start


def remove_worktree(repo_root: Path, worktree_path: Path) -> bool:
    """Force-remove *worktree_path* from the git worktree registry. Returns True on success."""
    if _worktree_locked(worktree_path):
        logger.info("Skipping worktree removal — locked by live owner: %s", worktree_path)
        return False
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
        release_worktree_lock(worktree_path)
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

    alive_issues = _alive_dispatch_issue_numbers()

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
        meta = parse_agent_branch(branch)
        if meta and int(meta.issue) in alive_issues:
            logger.info(
                "Skipping worktree removal — dispatcher alive for issue %s: %s",
                meta.issue,
                wt_path,
            )
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
