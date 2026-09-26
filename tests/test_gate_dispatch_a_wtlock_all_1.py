"""Claim all-1: the gate lock and the fleet_ops lane lock are two different locks.

The PR serializes gate worktree mutation under a lock file in
``~/.agent-fleet/admission/locks/``. ``agent_fleet.fleet_ops.worktree`` has
already been serializing lane worktree creation per repository, but under
``.git/agent-fleet-worktree.lock`` (``_repo_lock``). Two lock files for the same
shared resource (git's ``.git/worktrees`` metadata) means a lane ``add`` and a
gate ``add``/``prune`` are *not* mutually exclusive — so the exact corruption the
PR describes (a prune deleting a sibling's half-registered worktree) is still
reachable between the two subsystems.

These tests exercise the real code paths against one real git repository:
``ensure_lane_worktree`` and ``prepare_worktree``/``worktree_lock``. They assert
the *correct* behaviour — one lock per repository, shared across both
subsystems — and therefore fail at this head.
"""

from __future__ import annotations

import subprocess
import threading
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from agent_fleet.fleet_ops.worktree import ensure_lane_worktree, worktree_root
from agent_fleet.gate.gitops import _lock_dir, _repo_key, prepare_worktree, worktree_lock


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
    root = tmp_path / "repo"
    root.mkdir()
    _git(root.parent, "init", "-q", "-b", "main", str(root))
    _git(root, "config", "user.email", "lock@test.local")
    _git(root, "config", "user.name", "Lock Test")
    (root / "f.txt").write_text("one\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep any gate lock files inside the test's tmp dir."""
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


def _gate_lock_path(repo: Path) -> Path:
    """The lock file the gate uses for *repo*, as it will exist on disk."""
    return (_lock_dir() / f"worktree-{_repo_key(repo)}.lock").resolve()


def _lane_lock_path(repo: Path) -> Path:
    """The lock file ``fleet_ops.worktree._repo_lock`` uses for *repo*."""
    return (worktree_root(repo) / ".git" / "agent-fleet-worktree.lock").resolve()


def test_gate_and_lane_worktree_creation_share_one_lock(repo: Path, tmp_path: Path) -> None:
    """Both subsystems mutate ``.git/worktrees``; they must take the same lock.

    One lock per repository means a lane's ``git worktree add`` cannot interleave
    with a gate's ``git worktree prune`` — the corruption the PR documents.
    """
    head = _git(repo, "rev-parse", "HEAD")

    # Real calls, so both lock files are really created on disk.
    ensure_lane_worktree(
        repo,
        lane="lane-a",
        base="main",
        target_path=tmp_path / "wt" / "lane-a",
        parent=tmp_path / "wt",
    )
    prepare_worktree(repo, tmp_path / "wt" / "gate-0", head)

    lane_lock = _lane_lock_path(repo)
    gate_lock = _gate_lock_path(repo)

    assert lane_lock.is_file(), f"the lane lock was never created at {lane_lock}"
    assert gate_lock.is_file(), f"the gate lock was never created at {gate_lock}"
    assert lane_lock == gate_lock, (
        f"gate locks {gate_lock} but fleet_ops locks {lane_lock}: gate and lane "
        "worktree creation are guarded by two different files, so a lane "
        "`git worktree add` can still interleave with a gate's `git worktree prune`"
    )


def test_lane_add_blocks_while_the_gate_holds_the_repo_lock(repo: Path, tmp_path: Path) -> None:
    """A lane add must queue behind the gate's in-flight worktree mutation.

    The gate holds the repository lock for the whole add/remove/prune. A lane
    launching against the same repository in that window has to wait; if it does
    not, the two are running ``git worktree`` against one ``.git`` concurrently,
    which is the lost-worktree failure this change exists to stop.
    """
    inside_gate = threading.Event()
    release_gate = threading.Event()
    lane_finished = threading.Event()
    errors: list[BaseException] = []

    def gate_holds_lock() -> None:
        with worktree_lock(repo):
            inside_gate.set()
            if not release_gate.wait(timeout=60):
                errors.append(AssertionError("gate never released; test hung"))

    def lane_runs() -> None:
        try:
            if not inside_gate.wait(timeout=60):
                errors.append(AssertionError("gate never took the lock; test proved nothing"))
                return
            ensure_lane_worktree(
                repo,
                lane="lane-b",
                base="main",
                target_path=tmp_path / "wt" / "lane-b",
                parent=tmp_path / "wt",
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            lane_finished.set()

    holder = threading.Thread(target=gate_holds_lock, daemon=True)
    holder.start()
    lane = threading.Thread(target=lane_runs, daemon=True)
    lane.start()

    try:
        # Watch for up to 15s: with a shared lock the lane must still be queued.
        queued = not lane_finished.wait(timeout=15)
    finally:
        release_gate.set()
        holder.join(timeout=30)
        lane.join(timeout=30)

    assert inside_gate.is_set(), "the gate never acquired its lock; the test proved nothing"
    assert not errors, f"a lane add failed: {errors}"
    assert queued, (
        "ensure_lane_worktree finished while the gate still held the repository "
        "worktree lock: the two subsystems take different locks, so gate and lane "
        "worktree mutation are not mutually exclusive"
    )
    assert (tmp_path / "wt" / "lane-b").is_dir(), "the lane worktree was not created"
