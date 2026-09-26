"""correctness-1: a plain waiter behind a long-held lock is not a deadlock.

The only definition the module gives of a deadlock (locks.py:26-30) is
mutual: "a ``waiting`` record naming a lock another component ``holds``, where
that holder is in turn ``waiting`` for a lock this one holds."

``LockRegistry.hold()`` writes exactly that pair on contention -- a ``waiting``
record naming the contested lock, and a ``held`` record naming whoever won it --
and that is *not* a cycle; it is an ordinary, healthy queue. ``deadlocks()`` only
requires ``len(path) >= 2`` (locks.py:401), so a two-element
``[waiter, holder]`` path qualifies. The edge-age filter does not save it
either: the oldest edge is ``records[r.waiting_for].age_s(now)``, which for the
sole waiting record reads the *held* record's acquisition age (locks.py:403-410)
rather than how long the waiter has been queued -- so any lock held for longer
than the threshold makes its queue look like a deadlock.

Consequence: watchdog rule (d) fires on healthy contention and the remediation
releases a live, held lock, destroying the mutual exclusion the flock was there
to provide.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.locks import LockRegistry
from agent_fleet.serve.paths import exclusive_lock

if TYPE_CHECKING:
    from pathlib import Path

NOW = 1_000_000.0
LOCK = "repo-x.merge"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path / "home" / "runs"))


def test_a_healthy_merger_holding_a_lock_with_one_waiter_is_not_a_deadlock() -> None:
    reg = LockRegistry("op")

    # The holder takes the real flock, exactly as hold() would.
    reg.directory.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(reg.flock_for(LOCK)) as acquired:
        assert acquired
        reg.mark_held(LOCK, holder="merger", pid=os.getpid(), starttime=None, now=NOW - 7200)
        # ...and a second component contends for it 0.1s ago. hold() writes this
        # pair: the holder's record, then the waiter's intent naming the lock.
        reg.mark_waiting(LOCK, holder="dispatcher", pid=os.getpid(), starttime=None, now=NOW - 0.1)

        cycles = reg.deadlocks(now=NOW, threshold_minutes=20)

    assert not cycles, (
        f"a plain waiter queued behind a live holder is not a deadlock, but "
        f"deadlocks() reported {[[r.name for r in c] for c in cycles]}. The two-element "
        f"[waiting, held] path satisfies len(path) >= 2, and the edge-age filter reads "
        f"the held record's age rather than the wait's, so any lock held longer than "
        f"the threshold makes its queue look deadlocked."
    )


def test_control_a_real_two_holder_cycle_is_still_detected() -> None:
    """The walk still finds genuine mutual waits, so the fix is a shape check."""
    reg = LockRegistry("op")
    reg.mark_held("a-lock", holder="merger", pid=os.getpid(), starttime=None, now=NOW - 3600)
    reg.mark_waiting("a-lock", holder="dispatcher", pid=os.getpid(), starttime=None, now=NOW - 3600)
    reg.mark_held("b-lock", holder="dispatcher", pid=os.getpid(), starttime=None, now=NOW - 3600)
    reg.mark_waiting("b-lock", holder="merger", pid=os.getpid(), starttime=None, now=NOW - 3600)

    cycles = reg.deadlocks(now=NOW, threshold_minutes=20)
    names = {record.name for cycle in cycles for record in cycle}
    assert {"a-lock", "b-lock"} <= names, f"precondition: the real cycle is found, got {cycles}"
