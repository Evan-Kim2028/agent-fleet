"""prodsafety-2: ``ItemBoard.append`` promises an fsync and never performs one.

The docstring is not incidental wording. It is titled "One line, appended,
fsync'd" and the next sentence gives the reason in terms of the failure it is
supposed to prevent: "The fsync matters more here than anywhere else in serve:
the board is written by components that may be killed by the watchdog moments
later, and a stage transition that never reached disk is a lane the supervisor
believes is still queued."

The body only calls ``handle.flush()``, which pushes the Python buffer into the
OS page cache. That survives a process kill but not a machine crash or power
loss, which is exactly the case the docstring is arguing about. ``items.py``
does not even import ``os``, so there is no fsync anywhere on the path.

This is a documented guarantee that does not exist, not a house-style
difference: the established pattern in this same repository is
``agent_fleet/orchestration/journal.py``, which does call ``os.fsync`` on its
append handle for the same stated reason.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.items import STAGE_QUEUED, ItemBoard, Transition

if TYPE_CHECKING:
    import pytest


def test_append_fsyncs_the_line_it_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)

    board = ItemBoard(tmp_path / "items.jsonl", clock=FakeClock(start_time=1_000.0))
    board.append(
        Transition(item_id="repo-a#1", stage=STAGE_QUEUED, epoch=1_000.0, component="dispatcher")
    )

    assert calls, (
        "ItemBoard.append() wrote the transition without ever calling os.fsync; "
        "handle.flush() only reaches the OS page cache, so a transition can be "
        "lost on power loss even though the docstring states it is 'appended, "
        "fsync'd' and argues that losing one means 'a lane the supervisor "
        "believes is still queued'"
    )


def test_the_documented_fsync_pattern_exists_elsewhere_in_this_repo() -> None:
    """Pin that the fix has an in-repo precedent rather than inventing a style."""
    journal = (
        Path(__file__).resolve().parent.parent / "agent_fleet" / "orchestration" / "journal.py"
    ).read_text(encoding="utf-8")
    assert "os.fsync(" in journal, (
        "expected the append-then-fsync pattern in orchestration/journal.py; if "
        "that moved, re-derive the pattern for serve/items.py before changing it"
    )
