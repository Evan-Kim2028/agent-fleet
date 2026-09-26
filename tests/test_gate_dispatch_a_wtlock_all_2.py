"""Claim all-2: ``_repo_key`` leaks remote credentials into the lock filename.

``_repo_key`` slugs the *raw* ``remote.origin.url``. A remote of the shape

    https://x-access-token:<PAT>@github.com/acme/widgets.git

is the normal shape after ``gh auth setup-git`` / a token-bearing clone, and it
puts the token into a persistent, human-readable file name under
``~/.agent-fleet/admission/locks/``. The codebase already has the credential-free
helper for this: ``agent_fleet.fleet_ops.binding.parse_remote_slug`` returns
``acme/widgets`` for exactly that URL.

These tests drive the real ``prepare_worktree`` / ``_repo_key`` code path against
a real git repository whose ``origin`` carries a token, and assert that no secret
material is written to disk.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from agent_fleet.fleet_ops.binding import parse_remote_slug
from agent_fleet.gate.gitops import _lock_dir, _repo_key, prepare_worktree

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
