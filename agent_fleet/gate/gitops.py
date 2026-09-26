"""Git and GitHub plumbing for the gate: resolve a PR head, run the gate's worktrees.

The gate never operates on the caller's checkout. It creates a detached
worktree at the PR head so that a reviewer agent, a verifier writing a new test
file, and a fixer committing and pushing all work against a known, immutable
commit — and the operator's own working tree is never dirtied by a run.

Every worktree is created with ``--detach`` at an explicit sha. A gate run that
crashed mid-round leaves a directory behind but no branch to half-update, and
the next run removes and recreates it.

**Worktree mutation is serialized per repository** (:func:`worktree_lock`).
``git worktree add`` and ``git worktree prune`` are not safe to run concurrently
against one repository: prune deletes the administrative entries under
``.git/worktrees`` for directories it cannot find, including a sibling gate's
worktree that ``add`` has registered but not yet finished populating. The
observed failure was ``fatal: could not write new index file`` /
``could not open '.git/worktrees/<name>/locked' for writing: No such file or
directory``, which loses the sibling's worktree outright. The lock covers add,
remove, *and* prune together — locking only ``add`` would still let one gate's
prune run while another's add is mid-flight, which is the actual corruption.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 - used at runtime (path.exists/is_file)

from agent_fleet.gate.pytest_runner import is_test_file

logger = logging.getLogger(__name__)

#: Per-repo re-entrancy, tracked per **thread**. ``prepare_worktree`` calls
#: ``remove_worktree``, which would otherwise self-deadlock on the same flock
#: (flock is per open file description, and a second ``open`` is a *different*
#: description, so it blocks). A plain process-wide counter would not do: it
#: cannot tell threads apart, so a second thread would assume the lock was
#: already held and walk straight into a concurrent worktree add.
_DEPTH = threading.local()

#: One mutex per repo key, so threads inside one gate process queue.
_MUTEX_LOCK = threading.Lock()
_MUTEXES: dict[str, threading.Lock] = {}


class GateError(RuntimeError):
    """A gate run cannot proceed (bad PR, unusable worktree, failed git call)."""


@dataclass(frozen=True)
class PullRequestRef:
    """The PR's head, as ``gh`` reports it."""

    number: int
    head_ref: str
    head_sha: str
    state: str
    base_ref: str = ""

    @property
    def short_sha(self) -> str:
        return self.head_sha[:9]

    @property
    def is_open(self) -> bool:
        return self.state.upper() == "OPEN"


def _run_git(repo: Path, *args: str, check: bool = True) -> str:
    """Run a git command in *repo*; raise :class:`GateError` on failure."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GateError(f"git {' '.join(args)} failed to launch: {exc}") from exc
    if check and completed.returncode != 0:
        raise GateError(
            f"git {' '.join(args)} exited {completed.returncode}: "
            f"{(completed.stderr or completed.stdout).strip()[:300]}"
        )
    return completed.stdout or ""


def resolve_pull_request(repo: Path, pr_number: int) -> PullRequestRef:
    """Resolve PR *pr_number* to its head ref/sha via the ``gh`` CLI.

    ``gh`` talks to the forge directly, so this works for a PR whose head
    branch lives in a fork and is not fetched locally.
    """
    if shutil.which("gh") is None:
        raise GateError("gh CLI not found; cannot resolve the PR head")
    try:
        completed = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--json",
                "headRefName,headRefOid,state,baseRefName",
            ],
            capture_output=True,
            text=True,
            cwd=repo,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GateError(f"gh pr view {pr_number} failed to launch: {exc}") from exc
    if completed.returncode != 0:
        raise GateError(
            f"gh pr view {pr_number} exited {completed.returncode}: "
            f"{(completed.stderr or '').strip()[:300]}"
        )
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise GateError(f"gh pr view {pr_number} returned unparseable JSON: {exc}") from exc
    return PullRequestRef(
        number=int(pr_number),
        head_ref=str(data.get("headRefName") or ""),
        head_sha=str(data.get("headRefOid") or ""),
        state=str(data.get("state") or ""),
        base_ref=str(data.get("baseRefName") or ""),
    )


def current_pr_head(repo: Path, pr_number: int) -> str:
    """Re-read the PR's head sha — how the gate notices a fixer's push."""
    return resolve_pull_request(repo, pr_number).head_sha


def _repo_key(repo: Path) -> str:
    """A stable per-repository lock name: its ``origin`` slug when it has one.

    The slug is read from the repo's own remote so two checkouts of the same
    repository share one lock — a gate worktree under ``~/Documents`` and one
    under ``/srv`` are the same repository and must not race. A repo with no
    ``origin`` (or a local-only test fixture) falls back to a hash of its
    resolved path.
    """
    try:
        url = _run_git(repo, "config", "--get", "remote.origin.url", check=False).strip()
    except GateError:  # pragma: no cover - _run_git only raises on launch failure
        url = ""
    if url:
        slug = re.sub(r"[^A-Za-z0-9]+", "-", url).strip("-").lower()
        if slug:
            return slug[-80:]
    resolved = str(repo.expanduser().resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    return f"path-{digest}"


def _lock_dir() -> Path:
    from agent_fleet.fleet_ops.admission import AdmissionConfig

    return AdmissionConfig().locks_dir()


def _mutex_for(key: str) -> threading.Lock:
    """The per-key thread mutex, created on first use."""
    with _MUTEX_LOCK:
        mutex = _MUTEXES.get(key)
        if mutex is None:
            mutex = threading.Lock()
            _MUTEXES[key] = mutex
        return mutex


@contextlib.contextmanager
def worktree_lock(repo: Path):  # noqa: ANN201
    """Serialize worktree add/remove/prune for *repo* across processes and threads.

    Two layers, and both are needed:

    * a **per-key ``threading.Lock``**, so threads inside one gate process queue
      instead of racing;
    * an **``flock``** on a file, so separate *processes* queue — which is the
      case that actually lost worktrees, since gates are separate processes.

    Re-entrancy is tracked per thread, because ``prepare_worktree`` calls
    ``remove_worktree`` and that nesting must not deadlock against itself.

    Degrades to running unlocked if the lock file cannot be created: a gate that
    cannot take a lock is still better than a gate that refuses to run.
    """
    key = _repo_key(repo)
    held = getattr(_DEPTH, "held", None)
    if held is not None and key in held:
        # Re-entrant call on this very thread: the lock is already ours.
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return

    mutex = _mutex_for(key)
    with mutex:
        held = getattr(_DEPTH, "held", None)
        if held is None:
            held = {}
            _DEPTH.held = held
        held[key] = 1
        handle = None
        try:
            try:
                directory = _lock_dir()
                directory.mkdir(parents=True, exist_ok=True)
                handle = (directory / f"worktree-{key}.lock").open("a+")
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except OSError as exc:
                logger.warning(
                    "worktree lock unavailable for %s (%s); proceeding without it", repo, exc
                )
                if handle is not None:
                    with contextlib.suppress(OSError):
                        handle.close()
                    handle = None
            try:
                yield
            finally:
                if handle is not None:
                    with contextlib.suppress(OSError):
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    with contextlib.suppress(OSError):
                        handle.close()
        finally:
            held.pop(key, None)


def prepare_worktree(repo: Path, path: Path, sha: str) -> Path:
    """Create a detached worktree at *sha*, replacing any previous one at *path*."""
    with worktree_lock(repo):
        remove_worktree(repo, path)
        repo.parent.mkdir(parents=True, exist_ok=True)
        _run_git(repo, "worktree", "add", "--detach", str(path), sha)
    return path


def remove_worktree(repo: Path, path: Path) -> None:
    """Remove a gate worktree. Never raises — a missing worktree is fine."""
    with worktree_lock(repo):
        if not path.exists():
            _run_git(repo, "worktree", "prune", check=False)
            return
        _run_git(repo, "worktree", "remove", "--force", str(path), check=False)
        shutil.rmtree(path, ignore_errors=True)


def fetch_base(repo: Path, base_branch: str) -> None:
    """Fetch the base ref so ``git diff base...HEAD`` resolves in a fresh worktree."""
    _run_git(repo, "fetch", "--quiet", "origin", base_branch, check=False)
    _run_git(repo, "fetch", "--quiet", "origin", check=False)


def worktree_head_sha(worktree: Path) -> str:
    """The commit a gate worktree currently sits on (empty string on failure)."""
    return _run_git(worktree, "rev-parse", "HEAD", check=False).strip()


def resolve_diff_base(worktree: Path, base_branch: str) -> str:
    """The ref a PR diff is taken against: ``origin/<base>`` when it exists.

    The local ``<base>`` branch of the main checkout can be far behind the
    forge (seen: lake-of-rage local main hundreds of commits behind), and
    diffing against it pulls unrelated upstream files into "the PR's changes".
    :func:`fetch_base` refreshes ``origin/<base>`` first. Explicit remote refs
    and SHAs pass through; a repo with no ``origin`` falls back to the local branch.
    """
    explicit = base_branch.startswith(("origin/", "refs/"))
    if explicit or re.fullmatch(r"[0-9a-f]{7,40}", base_branch):
        return base_branch
    remote = f"origin/{base_branch}"
    probe = _run_git(worktree, "rev-parse", "--verify", "--quiet", remote, check=False).strip()
    return remote if probe else base_branch


def changed_test_files(worktree: Path, base_branch: str) -> list[str]:
    """Repo-relative ``test_*.py`` paths the PR changed and that still exist.

    Deliberately narrow: the gate re-runs the PR's *own* tests as step0, and
    widening that to the whole suite would turn one slow package into a gate
    timeout for reasons unrelated to the change.
    """
    diff = _run_git(
        worktree,
        "diff",
        "--name-only",
        f"{resolve_diff_base(worktree, base_branch)}...HEAD",
        check=False,
    )
    out: list[str] = []
    for line in diff.splitlines():
        rel = line.strip()
        if not rel or not is_test_file(rel):
            continue
        if (worktree / rel).is_file():
            out.append(rel)
    return out
