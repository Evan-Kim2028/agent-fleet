"""Claim spec-1: the flock in ``_append`` does not span the critical section.

``_append`` documents the read and the write as "one critical section"
(``agent_fleet/pr_owner.py`` lines 126-133). The guard is a ``flock`` taken on
lines 136-141, and it is released by the ``os.close(handle)`` in that same
block — before ``read_notes`` (line 143) and ``write_notes`` (line 144) have
run. So the lock covers nothing that matters: it is taken and dropped while the
function is still only preparing, and every operation it was meant to order
happens after it is gone.

This file tests that scope claim directly rather than through the symptoms,
so the failure is unambiguous: a competing writer takes an exclusive lock on the
notes file *while ``_append`` is in the middle of its read-modify-write*. If
the guard held, that attempt would be refused.

The probe uses a different file descriptor from the one ``_append`` uses, which
is exactly how a second process would contend for the same file. The probe is
shown to have teeth by ``test_the_lock_probe_can_detect_a_held_lock``, which
first proves the same technique reports ``BlockingIOError`` against a lock that
genuinely is held — otherwise a test that never blocked would prove nothing.
"""

from __future__ import annotations

import fcntl
import os
import threading
from typing import TYPE_CHECKING

import pytest

from agent_fleet import pr_owner
from agent_fleet.pr_owner import pr_notes_path, write_notes

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A scratch repo directory. The notes path is the only thing used here."""
    path = tmp_path / "repo"
    path.mkdir()
    return path


def _competing_exclusive_lock_was_refused(path: Path) -> bool:
    """Try to take ``LOCK_EX`` on *path* from a fresh descriptor.

    Returns ``True`` when the kernel refused us, which is the answer a correctly
    scoped lock gives while another writer is inside the critical section.
    """
    handle = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        return False
    finally:
        os.close(handle)


def test_the_lock_probe_can_detect_a_held_lock(repo: Path) -> None:
    """Control for the probe below, so a green/false result means something.

    A probe that silently never blocks would report the same result whether or
    not the lock is held. This pins the technique against a lock that really is
    held on a different descriptor.
    """
    write_notes(repo, 7, "seed")
    path = pr_notes_path(repo, 7)

    holder = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX)
        assert _competing_exclusive_lock_was_refused(path) is True
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)


def test_the_notes_stay_locked_for_the_whole_read_modify_write(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While ``_append`` is reading and rewriting the notes, they must be locked.

    The check runs from inside ``write_notes``, i.e. after ``_append`` has read
    the base and is about to commit its new copy. At head the lock was dropped
    in the block above, so the notes are free and this competitor walks in.
    """
    write_notes(repo, 7, "seed")
    path = pr_notes_path(repo, 7)
    observed: dict[str, bool] = {}

    real_write = pr_owner.write_notes

    def _observe_then_write(*args: object, **kwargs: object) -> Path:
        observed["refused"] = _competing_exclusive_lock_was_refused(path)
        return real_write(*args, **kwargs)

    monkeypatch.setattr(pr_owner, "write_notes", _observe_then_write)

    errors: list[BaseException] = []

    def _append() -> None:
        try:
            pr_owner._append(repo, 7, "### Round A\n")
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=_append)
    thread.start()
    thread.join(timeout=60)
    assert not thread.is_alive(), "_append never returned"

    assert not errors, f"_append raised: {[type(e).__name__ for e in errors]}"
    assert observed.get("refused") is True, (
        "the notes were not locked during _append's read-modify-write: a second "
        "writer could take LOCK_EX on them while this round was still writing, so "
        "the read-then-write is not the one critical section the docstring claims"
    )
