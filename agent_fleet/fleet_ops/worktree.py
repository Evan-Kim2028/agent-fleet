"""Isolated worktrees for lanes.

Each lane gets its own worktree on its own branch, so two operator sessions can
work the same repo simultaneously without seeing each other's uncommitted
changes. This mirrors what the bash drivers got for free from a hardcoded path
convention (``$HOME/Documents/$REPO-wt-fb-$LANE``); here the path is derived and
the worktree is **reused** when it already exists.

Reuse matters more than creation: a lane that was interrupted (killed mid-run,
or escalated) usually has real work in its worktree. Creating a fresh worktree
would silently abandon it. ``git worktree add`` would also fail outright on a
branch already checked out somewhere, so reuse is the only correct behaviour.
"""

from __future__ import annotations

import contextlib
import fcntl
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    Runner = Callable[..., "subprocess.CompletedProcess[str]"]

#: Default sibling-worktree root, matching the bash drivers' convention.
DEFAULT_WORKTREE_PARENT = "~/Documents"

#: Directory the engine writes its run logs into, relative to a worktree root.
RUN_DIR_NAME = ".agent-fleet"

#: The transcript subtree *inside* that directory, which is the only part that is
#: ever the manager's scratch output. The exclude line names this, not the
#: directory: ``.agent-fleet/`` as a whole also hides work a lane legitimately
#: produces in a repository that tracks that directory, and a file git cannot see
#: is a file the guarantee never stages.
RUN_LOGS_DIR_NAME = "runs"

#: The ``info/exclude`` entry that keeps run logs out of git. Written by
#: :func:`ensure_run_dir_excluded` on every lane worktree: an untracked log file
#: in a worktree is not "work", it is the manager's own scratch output, and the
#: PR guarantee stages with ``git add -A`` — so without this a lane commits its
#: own run log, and a lane that changed nothing commits *only* the log and then
#: fails on the repo's hooks.
RUN_DIR_EXCLUDE_LINE = f"{RUN_DIR_NAME}/{RUN_LOGS_DIR_NAME}/"

_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class WorktreeResult:
    """Where the lane's worktree is, and whether it was created or reused."""

    path: Path
    branch: str
    created: bool
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "branch": self.branch,
            "created": self.created,
            "reason": self.reason,
        }


def sanitize_component(value: str) -> str:
    """Make *value* safe as a single path component (a lane is operator input)."""
    cleaned = _SAFE_COMPONENT.sub("-", (value or "").strip()).strip("-.")
    return cleaned or "lane"


def default_worktree_path(repo_path: Path, lane: str, *, parent: Path | None = None) -> Path:
    """``<parent>/<repo>-wt-fb-<lane>`` — the bash drivers' layout."""
    root = Path(parent).expanduser() if parent else Path(DEFAULT_WORKTREE_PARENT).expanduser()
    repo_name = sanitize_component(repo_path.resolve().name)
    return root / f"{repo_name}-wt-fb-{sanitize_component(lane)}"


def _git(
    args: Sequence[str],
    *,
    cwd: Path,
    runner: Runner | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    run = runner or subprocess.run
    return run(list(args), cwd=cwd, capture_output=True, text=True, check=False, timeout=timeout)


def repo_root(path: Path, *, runner: Runner | None = None) -> Path:
    """The repository's main working tree root for *path*."""
    result = _git(["git", "rev-parse", "--show-toplevel"], cwd=path, runner=runner, timeout=60)
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip())
    return path.resolve()


def worktree_root(path: Path, *, runner: Runner | None = None) -> Path:
    """The main (non-bare) worktree root — what to run ``git worktree`` against."""
    result = _git(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=path,
        runner=runner,
        timeout=60,
    )
    if result.returncode == 0 and result.stdout.strip():
        common = Path(result.stdout.strip())
        # .../<repo>/.git -> <repo>
        if common.name == ".git":
            return common.parent
    return repo_root(path, runner=runner)


def list_worktrees(root: Path, *, runner: Runner | None = None) -> list[dict[str, str]]:
    """Parse ``git worktree list --porcelain`` into path/branch/HEAD records."""
    result = _git(["git", "worktree", "list", "--porcelain"], cwd=root, runner=runner, timeout=60)
    if result.returncode != 0:
        return []
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in result.stdout.splitlines():
        line = line.rstrip()
        if not line:
            if current:
                entries.append(current)
            current = {}
            continue
        if line.startswith("worktree "):
            current = {"path": line[len("worktree ") :]}
        elif line.startswith("branch "):
            ref = line[len("branch ") :]
            current["branch"] = ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD ") :]
    if current:
        entries.append(current)
    return entries


def find_worktree_for_branch(
    root: Path, branch: str, *, runner: Runner | None = None
) -> Path | None:
    """An existing worktree already checked out on *branch*, if any."""
    for entry in list_worktrees(root, runner=runner):
        if entry.get("branch") == branch and entry.get("path"):
            return Path(entry["path"])
    return None


def branch_exists(root: Path, branch: str, *, runner: Runner | None = None) -> bool:
    result = _git(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=root,
        runner=runner,
        timeout=60,
    )
    return result.returncode == 0


@contextlib.contextmanager
def _repo_lock(root: Path) -> Iterator[None]:
    """Serialize worktree creation per repository across processes and operators.

    Dozens of lanes launched in the same second otherwise race on git's shared
    metadata (.git/config.lock, worktrees/, index writes).
    """
    common = _git(
        ["git", "rev-parse", "--git-common-dir"], cwd=root, runner=None, timeout=60
    ).stdout.strip()
    lock_dir = (
        (root / common) if common and not Path(common).is_absolute() else Path(common or root)
    )
    lock_path = lock_dir / "agent-fleet-worktree.lock"
    with lock_path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _fresh_base(root: Path, base: str, *, runner: Runner | None = None) -> str:
    """The start point for a new lane branch: ``origin/<base>`` after a fetch, when it exists.

    A local ``main`` in a long-lived clone is routinely hundreds of commits behind
    (nobody pulls it; the main checkout may even hold it). Branching from it gives
    the implementer a stale tree and the PR a conflict-prone base. A fetch failure
    or a repo without that remote branch falls back to *base* as given.
    """
    if "/" in base:
        return base
    _git(["git", "fetch", "-q", "origin", base], cwd=root, runner=runner, timeout=300)
    remote = f"origin/{base}"
    probe = _git(
        ["git", "rev-parse", "--verify", "-q", f"refs/remotes/{remote}"],
        cwd=root,
        runner=runner,
        timeout=60,
    )
    return remote if probe.returncode == 0 else base


def ensure_run_dir_excluded(worktree: Path, *, runner: Runner | None = None) -> bool:
    """Add the run-log subtree to the repo's ``info/exclude``. Idempotent.

    The exclude file is per-*repository* and shared by every worktree, which is
    why this is written once per repo rather than per lane: one line serves all
    lanes, and a second call is a no-op rather than a duplicate. Returns whether
    the file was changed.

    The line names ``.agent-fleet/runs/`` and not ``.agent-fleet/``: a repository
    in this fleet tracks the latter as real config, and a blanket ignore hides
    files a lane creates there from ``git status`` entirely. An invisible file is
    never staged and never committed, and a lane whose only work was there read as
    a clean worktree and escalated ``no_commits_ahead`` on real work.

    ``info/exclude`` is used rather than a committed ``.gitignore`` on purpose —
    this is the manager's own scratch output, and adding it to the project's
    tracked files would be a change to the repo the lane is working *on*.
    """
    probe = _git(
        ["git", "rev-parse", "--git-path", "info/exclude"], cwd=worktree, runner=runner, timeout=60
    )
    if probe.returncode != 0 or not (probe.stdout or "").strip():
        return False
    raw = Path(probe.stdout.strip())
    target = raw if raw.is_absolute() else worktree / raw
    try:
        existing = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        existing = ""
    if any(line.strip() == RUN_DIR_EXCLUDE_LINE for line in existing.splitlines()):
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        prefix = "" if existing.endswith("\n") or not existing else "\n"
        with target.open("a", encoding="utf-8") as handle:
            handle.write(f"{prefix}{RUN_DIR_EXCLUDE_LINE}\n")
    except OSError:
        # The guarantee excludes the path at stage time regardless, so a repo
        # that will not take the exclude line is still correct.
        return False
    return True


def default_run_dir(operator: str, lane: str, *, run_id: str) -> Path:
    """``<fleet home>/runs/<operator>/<lane>/<run-id>`` — outside the worktree.

    Two reasons this is not under the worktree, both learned the hard way:

    * **Nothing scratch is ever in the branch.** The PR guarantee stages with
      ``git add -A``; anything it writes into the worktree becomes part of the
      PR unless it is excluded, and a repo's hooks then run against a commit
      that is only a log file.
    * **Runs do not overwrite each other.** ``<worktree>/.agent-fleet/runs/<lane>``
      is one directory per lane, so a second run of the same lane clobbered the
      first run's transcript — the one artifact an operator has when a lane
      needs explaining.

    *run_id* is supplied by the caller (a timestamp) precisely because the path
    cannot be derived from the lane alone any more. Note what is *not* a
    parameter: the worktree. The path must not depend on it.
    """
    from agent_fleet.fleet_paths import agent_fleet_home

    return (
        agent_fleet_home()
        / "runs"
        / sanitize_component(operator)
        / sanitize_component(lane)
        / sanitize_component(run_id)
    )


def ensure_lane_worktree(
    repo_path: Path,
    *,
    lane: str,
    branch: str | None = None,
    base: str = "main",
    target_path: Path | None = None,
    parent: Path | None = None,
    runner: Runner | None = None,
) -> WorktreeResult:
    """Create or reuse the lane's isolated worktree, and return where it is.

    Never removes or resets anything. If a worktree already holds the branch it
    is reused as-is, with whatever state it is in.

    Every path out of here also ensures the repo's ``info/exclude`` carries the
    run-dir entry, including the reuse paths: a worktree created by an older
    build still has the old in-worktree run dir on disk, and reusing it is
    exactly when that stale directory would get committed.
    """
    repo_path = Path(repo_path).expanduser().resolve()
    if not repo_path.is_dir():
        raise FileNotFoundError(f"repo path does not exist: {repo_path}")

    root = worktree_root(repo_path, runner=runner)
    branch = branch or f"fb/{lane}"
    path = target_path or default_worktree_path(repo_path, lane, parent=parent)

    existing = find_worktree_for_branch(root, branch, runner=runner)
    if existing is not None:
        ensure_run_dir_excluded(existing, runner=runner)
        return WorktreeResult(
            path=existing, branch=branch, created=False, reason="branch already checked out"
        )

    if path.is_dir():
        # A directory exists but the branch is not registered as a worktree —
        # adopt it if it is already a git worktree, otherwise report rather
        # than deleting whatever the operator left there.
        probe = _git(["git", "rev-parse", "--git-dir"], cwd=path, runner=runner, timeout=60)
        if probe.returncode == 0:
            current = _git(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path, runner=runner, timeout=60
            )
            current_branch = current.stdout.strip() if current.returncode == 0 else ""
            if current_branch == branch:
                ensure_run_dir_excluded(path, runner=runner)
                return WorktreeResult(
                    path=path, branch=branch, created=False, reason="adopted existing worktree"
                )
        raise FileExistsError(
            f"{path} exists and is not a worktree on {branch}; move it aside or pass --branch"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    if branch_exists(root, branch, runner=runner):
        args = ["git", "worktree", "add", str(path), branch]
        reason = "attached to existing branch"
    else:
        start = _fresh_base(root, base, runner=runner)
        # --no-track: with a remote start point git would write branch.<name>.merge into the shared
        # .git/config, and concurrent lane launches then fail on .git/config.lock.
        args = ["git", "worktree", "add", "--no-track", "-b", branch, str(path), start]
        reason = f"created from {start}"

    with _repo_lock(root):
        result = _git(args, cwd=root, runner=runner, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(
            f"git worktree add failed for {branch} at {path}: "
            f"{(result.stderr or result.stdout).strip()[:500]}"
        )
    ensure_run_dir_excluded(path, runner=runner)
    return WorktreeResult(path=path, branch=branch, created=True, reason=reason)
