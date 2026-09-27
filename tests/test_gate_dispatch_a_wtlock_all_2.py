"""Claim all-2: subsystems that mutate ``.git/worktrees`` do not take the lock.

The PR claims the lock is "shared by every subsystem that mutates
``.git/worktrees``". It is not: ``LocalGitOps.setup_workspace`` runs
``git worktree add`` and ``teardown_workspace`` runs ``git worktree remove
--force``, both unlocked, and both on live runner paths (``setup_workspace`` at
the IMPLEMENT phase, ``teardown_workspace`` at run end). A lock that covers the
gate but not these is a second flock over one shared resource, so the
lost-sibling-worktree race the PR documents stays reachable through a
subsystem it did not convert.

These tests drive the real ``LocalGitOps`` code against a real git repository
and assert that the worktree mutation is actually serialized against a lock
another subsystem holds.

The credential tests at the foot of this file cover a second property of the
same lock: a remote of the shape

    https://x-access-token:<PAT>@github.com/acme/widgets.git

is the normal shape after ``gh auth setup-git``, and the lock name must never
carry the token. The codebase already has the credential-free helper for this:
``agent_fleet.fleet_ops.binding.parse_remote_slug`` returns ``acme/widgets`` for
exactly that URL.
"""

from __future__ import annotations

import subprocess
import threading
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

from agent_fleet.fleet_ops.binding import parse_remote_slug
from agent_fleet.gate.gitops import _lock_dir, _repo_key, prepare_worktree, worktree_lock
from agent_fleet.integrations.local_git import LocalGitOps

TOKEN = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
REMOTE = f"https://x-access-token:{TOKEN}@github.com/acme/widgets.git"
#: The bare secret body: ``_repo_key`` lowercases the slug and rewrites every
#: character outside ``[A-Za-z0-9]`` (the ``_`` included) to ``-``, so matching on
#: this separator-free body proves the token itself reached the filename.
TOKEN_BODY = TOKEN.lower().split("_", 1)[1]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real repo whose ``origin`` embeds a token, as ``gh auth setup-git`` leaves it."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root.parent, "init", "-q", "-b", "main", str(root))
    _git(root, "config", "user.email", "lock@test.local")
    _git(root, "config", "user.name", "Lock Test")
    (root / "f.txt").write_text("one\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "remote", "add", "origin", REMOTE)
    return root


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


def test_repo_key_is_credential_free(repo: Path) -> None:
    """The lock key names the repository, not the secret used to reach it."""
    key = _repo_key(repo)
    assert TOKEN_BODY not in key.lower(), (
        f"_repo_key leaked the remote token into the lock name: {key!r}"
    )
    assert "x-access-token" not in key, (
        f"_repo_key leaked the remote user into the lock name: {key!r}"
    )
    assert key == "acme-widgets", (
        f"_repo_key returned {key!r}; the credential-free owner/repo slug "
        f"({parse_remote_slug(REMOTE)!r} -> 'acme-widgets') was available"
    )


def test_no_token_is_written_to_the_locks_directory(repo: Path, tmp_path: Path) -> None:
    """Creating a gate worktree must not persist the token on disk."""
    head = _git(repo, "rev-parse", "HEAD")
    prepare_worktree(repo, tmp_path / "wt" / "gate", head)
    assert (tmp_path / "wt" / "gate").is_dir()

    locks = _lock_dir()
    assert locks.is_dir(), "the gate created no lock directory; the test proved nothing"
    # Index by inode, not by name: a file's name is the thing under suspicion here.
    created = {p.stat().st_ino: p for p in locks.iterdir()}
    assert created, "the gate created no lock file; the test proved nothing"
    for path in created.values():
        assert TOKEN_BODY not in path.name.lower(), (
            f"the remote token was written into the lock file name {path.name!r} under {locks}"
        )
        assert TOKEN_BODY not in path.read_text(errors="replace").lower(), f"token inside {path}"


def test_lock_file_is_not_world_readable(repo: Path, tmp_path: Path) -> None:
    """A lock file created from a credential-bearing remote must be owner-only."""
    head = _git(repo, "rev-parse", "HEAD")
    prepare_worktree(repo, tmp_path / "wt" / "gate", head)

    locks = _lock_dir()
    created = [p for p in locks.iterdir() if p.is_file()]
    assert created, "no lock file was created; the test proved nothing"
    for path in created:
        mode = path.stat().st_mode & 0o777
        assert mode & 0o077 == 0, f"lock file {path} is readable beyond its owner: mode {mode:o}"


@pytest.fixture
def local_repo(tmp_path: Path) -> Path:
    """A plain local repo, with no ``origin`` — the runner's normal checkout."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root.parent, "init", "-q", "-b", "main", str(root))
    _git(root, "config", "user.email", "lock@test.local")
    _git(root, "config", "user.name", "Lock Test")
    (root / "f.txt").write_text("one\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _assert_blocks_while_locked(repo: Path, call: Callable[[], None], subject: str) -> None:
    """*call* must not complete while another subsystem holds the repo lock."""
    inside = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []

    def holder() -> None:
        with worktree_lock(repo):
            inside.set()
            if not release.wait(timeout=60):
                errors.append(AssertionError("lock holder never released; test hung"))

    def worker() -> None:
        try:
            if not inside.wait(timeout=60):
                errors.append(AssertionError("lock was never taken; test proved nothing"))
                return
            call()
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    held = threading.Thread(target=holder, daemon=True)
    held.start()
    doing = threading.Thread(target=worker, daemon=True)
    doing.start()

    try:
        ran = finished.wait(timeout=15)
    finally:
        release.set()
        held.join(timeout=30)
        doing.join(timeout=30)

    assert inside.is_set(), "the lock was never taken; the test proved nothing"
    assert not errors, f"{subject} raised while queued on the lock: {errors}"
    assert not ran, (
        f"{subject} completed while another subsystem still held the repository "
        f"worktree lock: it mutates the shared .git/worktrees registry without "
        f"taking the lock every other subsystem takes"
    )


def test_setup_workspace_waits_for_the_repository_lock(local_repo: Path, tmp_path: Path) -> None:
    """``LocalGitOps.setup_workspace`` must queue behind the repository lock.

    This is the runner's IMPLEMENT phase: it runs ``git worktree add`` against
    the shared ``.git/worktrees`` registry, so a gate's concurrent ``prune``
    could delete its half-registered entry even with the gate itself locked.
    """
    ops = LocalGitOps(local_repo, use_worktree=True, worktree_base=tmp_path / "wt")

    def add() -> None:
        ops.setup_workspace(local_repo, "run-1", "main", branch_name="fleet/run-1")

    _assert_blocks_while_locked(local_repo, add, "setup_workspace")

    target = tmp_path / "wt" / "run-1"
    assert target.is_dir(), "the workspace was never created once the lock was free"
    assert _git(local_repo, "worktree", "list", "--porcelain").count("worktree ") >= 2, (
        "the workspace was not registered in the shared registry"
    )


def test_teardown_workspace_waits_for_the_repository_lock(local_repo: Path, tmp_path: Path) -> None:
    """``LocalGitOps.teardown_workspace`` must take the same lock.

    It runs ``git worktree remove --force`` against the same registry, so an
    unlocked removal is the other half of the same race — and the more damaging
    half, since a remove takes a *sibling's* entry with it.
    """
    ops = LocalGitOps(local_repo, use_worktree=True, worktree_base=tmp_path / "wt")
    target = ops.setup_workspace(local_repo, "run-2", "main", branch_name="fleet/run-2")
    assert target.is_dir()

    def remove() -> None:
        ops.teardown_workspace(target)

    _assert_blocks_while_locked(local_repo, remove, "teardown_workspace")

    ops.teardown_workspace(target)
    assert not target.exists(), "the workspace was not removed once the lock was free"
