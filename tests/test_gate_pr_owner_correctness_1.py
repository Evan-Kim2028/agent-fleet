"""Claim correctness-1: ``_append`` releases its flock before the work it guards.

The claim: ``_append`` opens the notes, takes ``LOCK_EX``, and closes the file
descriptor in the same ``with`` block (``agent_fleet/pr_owner.py`` lines 136-141),
so the exclusive lock is already released before ``read_notes`` (line 143) and
``write_notes`` (line 144) run. The read-modify-write is therefore unprotected:
concurrent appends to the *same* PR read the same base and the later writer
splices its stale copy over the earlier round's block.

Two independent consequences are asserted here, both on the real ``_append``:

1. Rounds are lost. Six threads append to one PR; the notes must end up
   containing all six blocks. They do not.
2. The round is never recorded at all. ``write_notes``'s temp name is
   ``notes.md.<pid>.tmp`` — one path per *process*, not per writer — so two
   threads in one process share it. The loser's ``tmp.replace`` then raises
   ``FileNotFoundError`` and ``_append`` propagates it, and that round leaves
   nothing behind in the notes.

The schedule is pinned rather than raced: a barrier holds every thread after
its read, so all N threads necessarily read the same base before any of them
writes. That is a schedule a real scheduler can produce on any loaded host; it
is not an artificial one. The existing
``test_two_concurrent_appends_both_survive`` cannot catch this because it
appends to PRs 7 and 8 — two different files, and flock is not the thing
serialising them.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest

from agent_fleet import pr_owner
from agent_fleet.pr_owner import read_notes, write_notes

if TYPE_CHECKING:
    from pathlib import Path

ROUNDS = 6

#: How long a round waits for its peers to finish reading. At head every thread
#: reaches this gate within microseconds of the others, so a short bound is
#: ample; it is kept small only so that an implementation which *does* serialise
#: the appends (where one thread at a time is inside the critical section) does
#: not stall the suite.
GATE_TIMEOUT_S = 0.5


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A scratch repo directory — the notes path is the only thing used here."""
    path = tmp_path / "repo"
    path.mkdir()
    return path


def _concurrent_appends(
    repo: Path, pr_number: int, rounds: int, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[str], list[BaseException]]:
    """Append *rounds* blocks to one PR with every read pinned to the same base.

    ``read_notes`` is wrapped, not ``_append``: the replacement returns exactly
    what the real reader returns, so the only thing it changes is *when* the
    read happens relative to the other threads' writes.
    """
    real_read = pr_owner.read_notes
    seen = 0
    counter_lock = threading.Lock()
    all_read = threading.Event()

    def _pinned_read(*args: object, **kwargs: object) -> str:
        nonlocal seen
        result = real_read(*args, **kwargs)
        with counter_lock:
            seen += 1
            if seen >= rounds:
                all_read.set()
        # Every append now blocks here until all `rounds` reads have happened,
        # which is exactly the interleaving the lock is supposed to prevent.
        all_read.wait(timeout=GATE_TIMEOUT_S)
        return result

    monkeypatch.setattr(pr_owner, "read_notes", _pinned_read)

    names = [f"Round {i}" for i in range(rounds)]
    errors: list[BaseException] = []

    def _append(name: str) -> None:
        try:
            pr_owner._append(repo, pr_number, f"### {name}\n\n- fixed: 1")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_append, args=(name,)) for name in names]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    for thread in threads:
        assert not thread.is_alive(), "a _append call never returned"

    monkeypatch.undo()
    return names, errors


def test_concurrent_appends_to_one_pr_lose_rounds(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two rounds on the same PR must both survive: the read and the write are
    one critical section. At head the lock is gone before either runs, so every
    thread reads the same base and only the last writer's block is left."""
    write_notes(repo, 7, "seed")
    names, _errors = _concurrent_appends(repo, 7, ROUNDS, monkeypatch)

    notes = read_notes(repo, 7)
    lost = [name for name in names if f"### {name}" not in notes]
    assert not lost, (
        f"{len(lost)} of {ROUNDS} rounds were erased from the notes: {lost}; "
        f"the surviving notes are:\n{notes}"
    )


def test_a_concurrent_append_that_raises_still_records_its_round(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The collision is not just a lost block: the whole call fails.

    ``write_notes`` names its temp file by PID alone, so every thread in this
    process writes the same ``notes.md.<pid>.tmp``. One thread's replace moves
    it away and the loser's replace raises ``FileNotFoundError``, which
    ``_append`` propagates. A round that ran, fixed things, and pushed is then
    absent from the notes entirely — the next round cannot know it happened.
    """
    write_notes(repo, 7, "seed")
    names, errors = _concurrent_appends(repo, 7, ROUNDS, monkeypatch)

    assert not errors, (
        f"{len(errors)} of {ROUNDS} _append calls raised: "
        f"{[type(e).__name__ for e in errors]}; the notes are:\n{read_notes(repo, 7)}"
    )
    notes = read_notes(repo, 7)
    missing = [name for name in names if f"### {name}" not in notes]
    assert not missing, f"rounds that returned without being recorded: {missing}"
