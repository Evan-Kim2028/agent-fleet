"""prodsafety-4: a failed acquisition must not destroy the holder's ``held`` record.

``hold()`` calls ``mark_waiting`` on failure, and both ``mark_waiting`` and
``mark_held`` write the single record file ``locks/<name>.json`` (locks.py:282
vs :293). So the moment a second component contends for a lock, the holder's
``held`` record is overwritten by a ``waiting`` record, and ``stale_locks()``
-- which filters on ``record.state != STATE_HELD`` -- can no longer see the
dead holder it was written to find.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.locks import STATE_HELD, STATE_WAITING, LockRegistry

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path / "home" / "runs"))


def _hold_flock_elsewhere(flock: Path) -> subprocess.Popen[bytes]:
    src = (
        "import fcntl, os, sys, time\n"
        "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o644)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "print('held', flush=True)\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", src, str(flock)],
        stdout=subprocess.PIPE,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == b"held"
    return proc


def test_stale_lock_stays_detectable_after_a_contender_contends() -> None:
    reg = LockRegistry("op")
    now = time.time()
    name = "merge:acme"

    # A dead holder, an hour old: exactly what rule (c) exists to find.
    reg.mark_held(name, holder="merger", pid=999999, starttime=1, now=now - 3600)
    assert [r.name for r in reg.stale_locks(now=now, grace_minutes=0)] == [name], (
        "precondition: the dead holder's record is reported as stale"
    )

    # Someone else contends for the same lock, and loses.
    proc = _hold_flock_elsewhere(reg.flock_for(name))
    try:
        with reg.hold(name, holder="dispatcher", pid=os.getpid(), starttime=1, now=now) as got:
            assert got is False, "the flock should have refused the second holder"

            record = reg.read(name)
            assert record is not None
            assert record.state == STATE_HELD and record.holder == "merger", (
                f"a failed acquisition clobbered the holder's record: state="
                f"{record.state!r} holder={record.holder!r}. The waiter's intent "
                "must be recorded separately from the holder's record."
            )
            assert STATE_WAITING not in {record.state}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)

    # The stale lock must still be visible after the contention episode.
    still = [r.name for r in reg.stale_locks(now=now, grace_minutes=0)]
    assert name in still, (
        "watchdog rule (c) can no longer see the dead holder, so the lock stays "
        "wedged: nothing retries, and the record is no longer 'held'"
    )
