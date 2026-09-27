"""contract-5: the production writer of ``waiting`` records must emit a graph edge.

``LockRegistry.hold`` is the only production caller of ``mark_waiting``
(locks.py:282), and it hardcodes ``waiting_for=None``. The deadlock walk at
line 334 requires ``start.waiting_for`` to be truthy before it will even begin,
so no cycle the module docstring promises can ever be detected from data the
registry itself produced.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.locks import STATE_WAITING, LockRecord, LockRegistry

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path / "home" / "runs"))


class _ExternalHolder:
    """Holds the flock from another process, so ``hold()`` really contends."""

    _SRC = (
        "import fcntl, os, sys, time\n"
        "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o644)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "print('held', flush=True)\n"
        "time.sleep(60)\n"
    )

    def __init__(self, flock: Path) -> None:
        flock.parent.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [sys.executable, "-c", self._SRC, str(flock)],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self.proc.stdout is not None
        assert self.proc.stdout.readline().strip() == "held"

    def close(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)


@pytest.fixture
def external_holder() -> Iterator[Callable[[Path], _ExternalHolder]]:
    made: list[_ExternalHolder] = []

    def _make(path: Path) -> _ExternalHolder:
        holder = _ExternalHolder(path)
        made.append(holder)
        return holder

    yield _make
    for holder in made:
        holder.close()


def test_failed_acquisition_through_the_public_api_records_no_edge(
    external_holder: Callable[[Path], _ExternalHolder],
) -> None:
    """``hold()`` on a contended lock yields False and writes an edgeless record."""
    reg = LockRegistry("op")
    external_holder(reg.flock_for("lane"))

    with reg.hold("lane", holder="A", pid=os.getpid(), starttime=1) as acquired:
        assert acquired is False, "the external holder should have blocked this"

        record = reg.read("lane")
        assert record is not None
        assert record.state == STATE_WAITING
        assert record.holder == "A"
        assert record.waiting_for is not None, (
            "hold() hardcodes waiting_for=None, so the record carries no graph "
            "edge and watchdog rule (d) can never fire from real data"
        )


def test_deadlock_detection_is_unreachable_from_production_writes() -> None:
    """Two real acquisitions produce a record set that ``deadlocks()`` ignores."""
    reg = LockRegistry("op")
    reg.directory.mkdir(parents=True, exist_ok=True)

    # A two-holder cycle as the docstring describes it: A waits for "lane"
    # (held by B) while B waits for "gate" (held by A). Both states are only
    # ever produced by hold(), so this is exactly the production data shape.
    with reg.hold("lane", holder="A", pid=os.getpid(), starttime=1) as _:
        pass
    with reg.hold("gate", holder="B", pid=os.getpid(), starttime=1) as _:
        pass

    records = reg.all_records()
    assert records, "no records were written at all"

    # Re-point the edges a correct hold() would have recorded, to show the walk
    # itself works, and that only the missing edge is what disables it.
    reg.write(
        LockRecord(
            name="lane",
            state=STATE_WAITING,
            holder="A",
            pid=os.getpid(),
            starttime=1,
            acquired_epoch=time.time() - 10_000,
            waiting_for="gate",
        )
    )
    reg.write(
        LockRecord(
            name="gate",
            state=STATE_WAITING,
            holder="B",
            pid=os.getpid(),
            starttime=1,
            acquired_epoch=time.time() - 10_000,
            waiting_for="lane",
        )
    )
    with_edge = reg.deadlocks(now=time.time(), threshold_minutes=0)
    assert with_edge, "the graph walk is broken too, which is a different defect"

    # Now the real thing hold() produces: no edges anywhere.
    reg.write(
        LockRecord(
            name="lane",
            state=STATE_WAITING,
            holder="A",
            pid=os.getpid(),
            starttime=1,
            acquired_epoch=time.time() - 10_000,
            waiting_for=None,
        )
    )
    reg.write(
        LockRecord(
            name="gate",
            state=STATE_WAITING,
            holder="B",
            pid=os.getpid(),
            starttime=1,
            acquired_epoch=time.time() - 10_000,
            waiting_for=None,
        )
    )
    assert reg.deadlocks(now=time.time(), threshold_minutes=0) == [], (
        "deadlock detection is only reachable with hand-built records; hold() "
        "never writes the edge, so rule (d) is dead code in production"
    )
