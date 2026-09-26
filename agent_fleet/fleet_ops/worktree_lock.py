"""The one lock that serializes ``git worktree`` mutation, per repository.

``git worktree add`` and ``git worktree prune`` are not safe to run concurrently
against one repository: prune deletes the administrative entries under
``.git/worktrees`` for directories it cannot find, including a sibling's
worktree that ``add`` has registered but not finished populating. The observed
failure was ``fatal: could not write new index file`` / ``could not open
'.git/worktrees/<name>/locked' for writing: No such file or directory``, which
loses the sibling's worktree outright.

The lock file therefore has to be **one per repository, shared by every
subsystem that mutates that registry** — gate runs, lane worktree creation, the
merge-compatibility probe, the PR loop. A gate lock under ``~/.agent-fleet`` and
a lane lock under ``.git`` would be two different flocks over one shared
resource, and a lane ``add`` could still interleave with a gate's ``prune``: the
corruption would survive the fix that documented it.

**Location: ``.git/agent-fleet-worktree.lock``.** A ``flock`` is visible to
every process on the box and needs no cleanup, so the file has to outlive a
crashed holder — which rules out a location under the gate's worktree parent,
because a worktree that is removed takes its whole administrative directory
with it. ``.git`` is also the only place that is guaranteed writable for a
clone nobody owns, and it is exactly the shared resource being guarded.

Nothing is ever written into the file: it carries no owner, no pid, no
credentials, and no text at all. The name carries the repository slug purely so
a human can tell whose lock they are looking at, and that slug is built from
:func:`agent_fleet.fleet_ops.binding.parse_remote_slug` — a token-bearing
``remote.origin.url`` (the normal shape after ``gh auth setup-git``) is reduced
to ``owner/repo`` before it can reach a filename on disk.

The in-process layer — a per-key ``threading.Lock`` and a per-thread depth
counter — exists because ``flock`` is held per *open file description*, not per
process: a second ``open`` of the same path inside one process is a different
description and blocks, so ``gitops.prepare_worktree`` calling
``gitops.remove_worktree`` would deadlock against itself. It also stops two
threads in one process from queueing on the kernel at all.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import logging
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from agent_fleet.fleet_ops.binding import parse_remote_slug

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

#: The lock file, inside the repository's administrative directory. It is
#: deliberately *not* ``.git/worktrees/<name>/...``: that whole directory is
#: removed when the worktree is removed, taking the lock with it.
LOCK_FILENAME = "agent-fleet-worktree.lock"

#: Owner-only. The file holds nothing, but the directory it lives in is created
#: by git with the process umask, and a lock another user's group can read is a
#: lock another user's group can swap.
LOCK_MODE = 0o600

#: Per-repo re-entrancy, tracked per **thread**.
_DEPTH = threading.local()

#: One mutex per repo key, so threads inside one process queue.
_MUTEX_LOCK = threading.Lock()
_MUTEXES: dict[str, threading.Lock] = {}


class RepoLockUnavailable(RuntimeError):
    """The repository has no administrative directory to hold the lock in.

    Distinct from an :class:`OSError` on a lock file: nothing is wrong with the
    filesystem, there is simply nowhere to put a lock, so a caller that has a
    degrade-gracefully policy can tell the two apart.
    """


def _git_common_dir(path: Path) -> Path:
    """*path*'s shared administrative directory (``.../repo/.git``), or raise.

    The common dir, not the per-worktree git dir: ``git rev-parse --git-dir``
    inside a worktree answers with ``.../repo/.git/worktrees/<name>``, a real
    *directory* but the wrong one — it is removed with the worktree, and a lock
    in it would be deleted out from under its own holder. It is a plain *file*
    when *path* is itself a linked worktree, which is why answering it needs
    the common dir at all.
    """
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    raw = result.stdout.strip() if result.returncode == 0 else ""
    if not raw:
        raise RepoLockUnavailable(f"no git administrative directory for {path}")
    common = Path(raw)
    if not common.is_absolute():
        common = path / common
    return common


def repo_lock_path(root: Path) -> Path:
    """Where the per-repository worktree lock for *root* lives.

    Raises :class:`RepoLockUnavailable` when *root* is not a git repository.
    """
    return _git_common_dir(Path(root).expanduser()) / LOCK_FILENAME


def _open_private(path: Path) -> int:
    """Open *path* for append, creating it owner-only, and return the fd."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, LOCK_MODE)
    try:
        os.fchmod(fd, LOCK_MODE)
    except OSError as exc:  # pragma: no cover - exotic filesystems only
        logger.warning("could not tighten permissions on %s (%s)", path, exc)
    return fd


@contextlib.contextmanager
def _flock(path: Path) -> Iterator[bool]:
    """Hold an exclusive ``flock`` on *path*; yield False if it cannot be taken.

    ``flock`` is released by the kernel when the holding process dies, so a
    crashed holder leaves nothing to clean up and no stale lock to break.
    """
    fd = -1
    try:
        fd = _open_private(path)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as exc:
        logger.warning("worktree lock %s unavailable (%s); proceeding without it", path, exc)
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        yield False
        return
    try:
        yield True
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)


def _admission_dir() -> Path:
    """``~/.agent-fleet/admission``, where the lock's admission pointer lives."""
    from agent_fleet.fleet_paths import agent_fleet_home

    return agent_fleet_home() / "admission"


def _write_admission_pointer(root: Path, path: Path) -> None:
    """Symlink an admission pointer at the lock file for *root*.

    The lock itself stays at ``<git-dir>/agent-fleet-worktree.lock`` because a
    ``flock`` is visible to every process on the box and so has to outlive a
    crashed holder — and a lock inside a gate *worktree* would be deleted out
    from under its holder the moment ``git worktree remove`` took the
    worktree's administrative directory with it.

    The pointer exists so ``<home>/admission/locks/`` still lists every
    repository the fleet has guarded, and ``readlink`` names which one. It is
    an index, not a second lock: a link replaces whatever link was there, so
    nothing can take the index entry believing the index itself is the lock.
    """
    directory = _admission_dir() / "locks"
    staging = directory / f".{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        staging.symlink_to(path)
        staging.replace(directory / f"worktree-{repo_key(root)}.lock")
    except OSError as exc:
        logger.warning("could not index the worktree lock in %s (%s)", directory, exc)
        with contextlib.suppress(OSError):
            staging.unlink()


@contextlib.contextmanager
def repo_worktree_lock(root: Path) -> Iterator[None]:
    """Serialize ``git worktree`` add/remove/prune for *root* across everything.

    Re-entrant per thread: nesting is a no-op, which is what lets
    ``prepare_worktree`` remove and then add under one held lock instead of
    releasing it in between — releasing in between would reopen exactly the
    window the lock exists to close.

    A lock that cannot be created degrades to running unlocked, with a warning.
    Only a caller that has nothing to degrade *to* (the lane path, whose caller
    has no other lock) turns that into a hard error; see :func:`require_worktree_lock`.
    """
    try:
        path = repo_lock_path(root)
        key = str(path.resolve())
    except RepoLockUnavailable as exc:
        logger.warning("no worktree lock for %s (%s); proceeding without it", root, exc)
        yield
        return
    # The re-entrancy check comes *before* the mutex. A threading.Lock is not
    # re-entrant, so nesting that took it twice would deadlock against itself:
    # prepare_worktree calls remove_worktree, and every nesting would have to
    # drop the mutex before the outer use of it could finish.
    if _held_on_this_thread(key):
        yield
        return
    with _mutex_for(key), _flock(path):
        _mark_held(key)
        try:
            yield
        finally:
            _clear_held(key)
            _write_admission_pointer(root, path)


def require_worktree_lock(root: Path) -> Path:
    """Return the lock path for *root*, or refuse.

    For a caller that must not proceed unlocked: a lane ``git worktree add``
    with no lock is the exact race that loses a sibling's worktree, and unlike
    a gate review it has no safe unlocked fallback.
    """
    path = repo_lock_path(root)
    if path.is_dir():
        raise OSError(f"worktree lock path {path} is a directory")
    return path


def _held_on_this_thread(key: str) -> bool:
    """Whether *this thread* already holds the lock for *key*."""
    return key in _depth()


def _depth() -> dict[str, int]:
    """This thread's held-key table, created on first use."""
    held: dict[str, int] | None = getattr(_DEPTH, "held", None)
    if held is None:
        held = {}
        _DEPTH.held = held
    return held


def _mark_held(key: str) -> None:
    _depth()[key] = _depth().get(key, 0) + 1


def _clear_held(key: str) -> None:
    depth = _depth()
    if depth[key] <= 1:
        depth.pop(key, None)
        return
    depth[key] -= 1


def _mutex_for(key: str) -> threading.Lock:
    """The per-key thread mutex, created on first use."""
    with _MUTEX_LOCK:
        mutex = _MUTEXES.get(key)
        if mutex is None:
            mutex = threading.Lock()
            _MUTEXES[key] = mutex
        return mutex


def origin_url(root: Path) -> str:
    """*root*'s ``remote.origin.url``, or the empty string when it has none."""
    result = subprocess.run(
        ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def repo_key(root: Path) -> str:
    """A stable, credential-free name for the repository at *root*.

    The name is read from the repository's own remote so two checkouts of the
    same repository agree on it — a gate worktree under ``~/Documents`` and one
    under ``/srv`` are the same repository and must not race. The raw
    ``remote.origin.url`` is never used: after ``gh auth setup-git`` it carries
    a live token, and a token must not end up in a filename. A repo with no
    ``origin`` (or a local-only test fixture) falls back to a hash of its
    resolved path.

    The name is documentation, not identity: the lock is keyed off its path, so
    two repositories that collided here would still hold separate locks.
    """
    key = credential_free_key(origin_url(root))
    if key:
        return key
    resolved = str(Path(root).expanduser().resolve())
    return f"path-{hashlib.sha256(resolved.encode('utf-8')).hexdigest()[:16]}"


def credential_free_key(url: str) -> str:
    """A stable, credential-free key naming the repository *url* points at.

    ``url`` is whatever ``git config --get remote.origin.url`` printed, which
    after ``gh auth setup-git`` is routinely
    ``https://x-access-token:<PAT>@github.com/owner/repo.git``. Slugging that
    raw string would write a live token into a persistent, human-readable
    filename. :func:`~agent_fleet.fleet_ops.binding.parse_remote_slug` reduces it
    to ``owner/repo`` first; the result is then slugged to ``owner-repo`` so it
    is usable as a single path component.
    """
    text = (url or "").strip()
    slug = parse_remote_slug(text) or _nameless_remote(text)
    safe = re.sub(r"[^A-Za-z0-9]+", "-", slug).strip("-").lower()
    return safe[-80:]


def _nameless_remote(text: str) -> str:
    """A name for a remote with no ``owner/repo`` in it, still secret-free.

    Covers the two remotes ``parse_remote_slug`` declines: a host-only URL
    (``https://github.com/onlyowner``) and a local path
    (``/srv/git/lake.git``), which is what a test fixture or an internal mirror
    actually uses. The userinfo is dropped before anything else, so a token
    sitting where the user belongs cannot survive into the name.
    """
    if "://" in text:
        _scheme, _, rest = text.partition("://")
        authority, _, path = rest.partition("/")
        host = authority.rpartition("@")[2]
        parts = [p for p in path.split("/") if p]
        return "/".join([host, *parts[-1:]])
    if "@" in text and ":" in text.partition("@")[0]:
        userinfo, _, path = text.partition("@")
        host = userinfo.partition(":")[0]
        parts = [p for p in path.split("/") if p]
        return "/".join([host, *parts[-1:]])
    return "/".join([p for p in text.split("/") if p][-2:])


__all__ = [
    "LOCK_FILENAME",
    "LOCK_MODE",
    "RepoLockUnavailable",
    "credential_free_key",
    "origin_url",
    "repo_key",
    "repo_lock_path",
    "repo_worktree_lock",
    "require_worktree_lock",
]
