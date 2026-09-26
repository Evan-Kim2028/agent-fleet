"""``ItemBoard.append`` must hand its transition to the device, not the page cache.

The docstring for ``ItemBoard.append`` in ``agent_fleet/serve/items.py`` opens
with 'One line, appended, fsync'd' and is explicit that the fsync is
load-bearing: the board is 'written by components that may be killed by the
watchdog moments later, and a stage transition that never reached disk is a
lane the supervisor believes is still queued'.

The body instead ends at ``handle.flush()``, which only copies the line into
the OS page cache.  A host-level loss (power cut, kernel panic) can therefore
discard a transition the supervisor was already told is on disk, and
``ItemBoard.items()`` / ``fold()`` reconstruct the item at an earlier,
non-terminal stage -- a shipped lane reads back as still queued.

How this is observed: the board's ``with self.path.open("a")`` handle is
captured by spying on the ``io.open`` that ``pathlib`` resolves (the C
``io.open`` caches ``os.open`` on first use, so patching ``os.open`` alone is
missed by an already-imported ``pathlib``).  ``os.fsync`` and ``os.fdatasync``
are then spied, calling straight through to the real syscalls.  Both raise
``EBADF`` for an fd that is not open for writing, so a pass cannot be
manufactured: the board can only be seen to fsync by really fsyncing a real,
live write fd -- and a stale fd is skipped rather than trusted.
"""

from __future__ import annotations

import io
import os
import pathlib
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

from agent_fleet.serve.items import STAGE_MERGED, STAGE_QUEUED, ItemBoard, fold, read_transitions

if TYPE_CHECKING:
    import pytest


class _BoardWatch:
    """Records the fd the board appends to and every fsync/fdatasync issued."""

    def __init__(self, board_path: Path) -> None:
        self.board_path = board_path
        self.append_fds: set[int] = set()
        self.fsynced: list[int] = []
        self.fdatasynced: list[int] = []
        self._real_io_open = io.open
        self._real_fsync = os.fsync
        self._real_fdatasync = os.fdatasync

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        watch = self

        def spy_io_open(*args, **kwargs) -> IO[Any]:  # noqa: ANN002, ANN003
            handle = watch._real_io_open(*args, **kwargs)
            target = args[0] if args else kwargs.get("file")
            try:
                if Path(target).samefile(watch.board_path) and "a" in str(handle.mode):
                    watch.append_fds.add(handle.fileno())
            except OSError, TypeError, ValueError:
                pass
            return handle

        def spy_fsync(fd: int) -> None:
            watch.fsynced.append(fd)
            watch._real_fsync(fd)

        def spy_fdatasync(fd: int) -> None:
            watch.fdatasynced.append(fd)
            watch._real_fdatasync(fd)

        monkeypatch.setattr(pathlib.io, "open", spy_io_open, raising=False)
        monkeypatch.setattr(os, "fsync", spy_fsync)
        monkeypatch.setattr(os, "fdatasync", spy_fdatasync)

    def durable(self) -> bool:
        """Did a live, recorded append fd reach the device before it closed?"""
        recorded = set(self.fsynced) | set(self.fdatasynced)
        return bool(recorded & self.append_fds)

    def attempts(self) -> str:
        return (
            f"append fds {sorted(self.append_fds)}, "
            f"fsync {self.fsynced!r}, fdatasync {self.fdatasynced!r}"
        )


def test_append_fsyncs_the_transition_before_returning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented contract: append returns with the line on the device."""
    board_path = tmp_path / "board.jsonl"
    board = ItemBoard(board_path)
    watch = _BoardWatch(board_path)
    watch.install(monkeypatch)

    board.record("lane-7", STAGE_QUEUED, repo="org/repo", pr=42)

    assert watch.append_fds, (
        "could not observe ItemBoard.append opening an append-mode handle on "
        f"{board_path}; the test cannot judge the durability contract"
    )
    assert watch.durable(), (
        "ItemBoard.append returned with the transition only in the page cache: "
        f"{watch.attempts()}. Its docstring promises 'One line, appended, "
        "fsync'd' precisely so a transition survives a component being killed "
        "by the watchdog moments later."
    )


def test_terminal_transition_is_durable_not_just_flushed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 'merged' line must reach the device, or fold() rewinds the lane."""
    board_path = tmp_path / "board.jsonl"
    board = ItemBoard(board_path)

    board.record("lane-9", STAGE_QUEUED, repo="org/repo")
    watch = _BoardWatch(board_path)
    watch.install(monkeypatch)

    board.record("lane-9", STAGE_MERGED, repo="org/repo", pr=99)

    assert watch.append_fds, (
        "could not observe the terminal transition being appended; the test "
        "cannot judge the durability contract"
    )
    assert watch.durable(), (
        "recording a terminal 'merged' transition issued no fsync/fdatasync of "
        f"the board fd ({watch.attempts()}); a power cut after record() returns "
        "drops that line and fold() reports the shipped lane as still queued."
    )
    # The gap is invisible to a reader precisely because the line does reach the
    # page cache -- which is exactly what the docstring must not promise.
    assert fold(read_transitions(board_path))["lane-9"].stage == STAGE_MERGED
