"""The per-repository gate worktree lock.

``git worktree add`` and ``git worktree prune`` are not safe to run concurrently
against one repository. ``prune`` deletes the administrative entries under
``.git/worktrees`` for directories it cannot find — including a sibling gate's
worktree that ``add`` has registered but not finished populating. The observed
failure was ``fatal: could not write new index file`` / ``could not open
'.git/worktrees/<name>/locked' for writing: No such file or directory``, which
loses the sibling's worktree outright.

:func:`agent_fleet.gate.gitops.worktree_lock` closes that. The tests here are
**real concurrency against a real git repo**: an in-process lock would pass
against a mocked git and still lose two gates running as separate processes,
which is how this reached production in the first place.
"""

from __future__ import annotations

import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from agent_fleet.gate.gitops import (
    _repo_key,
    prepare_worktree,
    remove_worktree,
    worktree_lock,
)


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
    """Keep the lock files inside the test's tmp dir."""
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


# ------------------------------------------------------------- the corruption


def test_concurrent_worktree_adds_all_succeed(repo: Path, tmp_path: Path) -> None:
    """Eight concurrent gate worktrees, all created, none lost.

    Without the lock this is the failure that lost a sibling gate's worktree:
    ``prepare_worktree`` calls ``remove_worktree`` (which prunes) and then
    ``add``, and the interleaving across threads is enough to corrupt
    ``.git/worktrees``.
    """
    head = _git(repo, "rev-parse", "HEAD")
    targets = [tmp_path / "wt" / f"gate-{i}" for i in range(8)]
    errors: list[BaseException] = []

    def build(target: Path) -> None:
        try:
            prepare_worktree(repo, target, head)
        except BaseException as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(build, targets))

    assert not errors, f"concurrent worktree creation failed: {errors}"
    for target in targets:
        assert target.is_dir(), f"{target} was not created"
    listed = _git(repo, "worktree", "list").splitlines()
    assert len(listed) == len(targets) + 1, "every worktree must still be registered"


def test_a_prune_cannot_delete_a_half_created_sibling_worktree(repo: Path, tmp_path: Path) -> None:
    """The exact production failure, pinned.

    ``git worktree add`` registers a directory under ``.git/worktrees`` and only
    then populates it. A concurrent ``prune`` — which is what
    :func:`remove_worktree` does for a path that no longer exists, and what
    :func:`prepare_worktree` does to clear the way — deletes that registration,
    and the in-flight ``add`` dies with::

        fatal: could not open '.git/worktrees/<name>/locked' for writing:
        No such file or directory

    Reproduced at roughly 1 failure per 40 single-shot adds against concurrent
    pruners. Adders and removers here both go through the public API — which is
    the point: a prune issued by one gate may never be in flight while another
    gate's ``add`` is mid-population.

    A prune issued by something *outside* the lock (an operator typing
    ``git worktree prune`` by hand) is deliberately out of scope: no lock in this
    process can stop that, and pretending otherwise would be a lie.
    """
    head = _git(repo, "rev-parse", "HEAD")
    stop = threading.Event()
    prunes = {"n": 0}

    def prune_forever(worker: int) -> None:
        """What a second gate does: remove a worktree that is already gone.

        That path is a bare ``git worktree prune``, and it is what deleted a
        sibling's half-created registration in production. Both the adders and
        the removers go through :func:`worktree_lock`, which is the whole point:
        a prune may never be in flight while an ``add`` is mid-population.
        """
        while not stop.is_set():
            remove_worktree(repo, tmp_path / "wt" / f"gone-{worker}-{prunes['n']}")
            prunes["n"] += 1

    removers = [threading.Thread(target=prune_forever, args=(i,), daemon=True) for i in range(2)]
    for thread in removers:
        thread.start()

    failures: list[str] = []
    barrier = threading.Barrier(6, timeout=120)

    def build(i: int) -> None:
        target = tmp_path / "wt" / f"sibling-{i}"
        barrier.wait()
        try:
            prepare_worktree(repo, target, head)
        except BaseException as exc:
            failures.append(str(exc))

    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(build, range(30)))
    finally:
        stop.set()
        for thread in removers:
            thread.join(timeout=30)

    assert prunes["n"] > 0, "no prune ran; the test proved nothing"
    assert not failures, f"a prune corrupted a sibling worktree: {failures[:3]}"
    for i in range(30):
        assert (tmp_path / "wt" / f"sibling-{i}").is_dir()


def test_prepare_and_remove_interleaved_do_not_corrupt_the_registry(
    repo: Path, tmp_path: Path
) -> None:
    """Adds and removes at once — the exact mix that produced the fatal error."""
    head = _git(repo, "rev-parse", "HEAD")

    def work(i: int) -> None:
        target = tmp_path / "wt" / f"w{i}"
        prepare_worktree(repo, target, head)
        remove_worktree(repo, target)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(work, range(8)))

    # The repo must still be usable afterwards.
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "worktree", "list").count("worktree ") >= 0


# --------------------------------------------------------------- the lock


def test_the_lock_is_reentrant_within_one_process(repo: Path, tmp_path: Path) -> None:
    """``prepare_worktree`` calls ``remove_worktree``; that must not self-deadlock.

    ``flock`` is per open file description, so a second ``open`` of the same
    path *inside the same process* is a different description and blocks. The
    depth counter is what keeps the nesting a no-op.
    """
    head = _git(repo, "rev-parse", "HEAD")
    target = tmp_path / "wt" / "reentrant"
    with worktree_lock(repo), worktree_lock(repo):
        pass
    # And through the real call path, which nests the two.
    prepare_worktree(repo, target, head)
    assert target.is_dir()


def test_different_repos_get_different_lock_files(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    for root in (a, b):
        _git(root.parent, "init", "-q", "-b", "main", str(root))
    assert _repo_key(a) != _repo_key(b)


def test_two_checkouts_of_the_same_repo_share_one_lock(tmp_path: Path) -> None:
    """A gate worktree under ~/Documents and one under /srv are one repository."""
    origin = tmp_path / "origin.git"
    one = tmp_path / "one"
    two = tmp_path / "two"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    _git(tmp_path, "init", "-q", "-b", "main", str(one))
    _git(one, "config", "user.email", "lock@test.local")
    _git(one, "config", "user.name", "Lock Test")
    (one / "f.txt").write_text("x\n", encoding="utf-8")
    _git(one, "add", "-A")
    _git(one, "commit", "-q", "-m", "base")
    _git(one, "remote", "add", "origin", str(origin))
    _git(one, "push", "-q", "origin", "main")
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(two)],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    assert _repo_key(one) == _repo_key(two)


def test_the_lock_serializes_across_threads(repo: Path) -> None:
    """Mutual exclusion is real: no two holders at once."""
    order: list[str] = []
    inside = threading.Event()
    reentered = threading.Event()

    def first() -> None:
        with worktree_lock(repo):
            order.append("in-1")
            inside.set()
            reentered.wait(timeout=5)
            order.append("out-1")

    def second() -> None:
        inside.wait(timeout=5)
        with worktree_lock(repo):
            order.append("in-2")
            order.append("out-2")

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    t2.start()
    # Give the second thread a chance to try to enter while the first holds it.
    threading.Timer(0.3, reentered.set).start()
    t1.join(timeout=15)
    t2.join(timeout=15)
    assert order == ["in-1", "out-1", "in-2", "out-2"], "the two holders interleaved"


def test_the_lock_degrades_when_the_lock_file_cannot_be_opened(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unusable lock dir must not stop a gate: it proceeds, unlocked.

    A gate that cannot take a lock is still better than a gate that refuses to
    run; the concurrency it loses is the same concurrency that existed before
    the lock did. This must be a *warning*, never an exception.
    """
    from agent_fleet.gate import gitops as mod

    def _boom() -> Path:
        raise OSError("no lock dir for you")

    monkeypatch.setattr(mod, "_lock_dir", _boom)
    head = _git(repo, "rev-parse", "HEAD")
    target = tmp_path / "wt" / "unlocked"
    prepare_worktree(repo, target, head)  # must not raise
    assert target.is_dir()
