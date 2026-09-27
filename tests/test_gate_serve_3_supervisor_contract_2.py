"""contract-2: deadlocks() must return the oldest cycle first.

``LockRegistry.deadlocks`` (locks.py:364) documents: "Returned oldest-edge-first
so the remediation releases the *older* claim, which is the one whose owner is
least likely to be making progress."

The walk iterates ``records.values()``, and ``all_records()`` returns a dict
built from ``sorted(self.directory.glob("*.json"))`` -- so the cycles come out
in *filename* order with no age sort at all. A deadlock whose edges are hours
old is therefore remediated after a deadlock whose edges are seconds old, which
inverts the rule: the fresh, probably-still-progressing claim is dropped first
and the stale, definitely-stuck one keeps the lock.

The test builds two disjoint 2-lock cycles whose file names and ages disagree:
the "zz" cycle has waited 9000s, the "aa" cycle 100s. Oldest-first ordering
puts "zz" first. Filename order puts "aa" first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.locks import LockRegistry

if TYPE_CHECKING:
    from pathlib import Path

NOW = 1_000_000.0


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path / "home" / "runs"))


def _two_lock_cycle(
    reg: LockRegistry, prefix: str, holder_a: str, holder_b: str, since: float
) -> None:
    """A waits for B's lock while holding its own, and B does the reverse."""
    reg.mark_held(f"{prefix}lock", holder=holder_a, pid=None, starttime=None, now=NOW - since)
    reg.mark_held(f"{prefix}held", holder=holder_b, pid=None, starttime=None, now=NOW - since)
    reg.mark_waiting(f"{prefix}lock", holder=holder_b, pid=None, starttime=None, now=NOW - since)
    reg.mark_waiting(f"{prefix}held", holder=holder_a, pid=None, starttime=None, now=NOW - since)


def test_deadlocks_are_returned_oldest_edge_first() -> None:
    reg = LockRegistry("op")
    # The older cycle sorts last by filename, so filename order and age order
    # disagree -- exactly the situation the docstring's rule exists for.
    _two_lock_cycle(reg, "zz", "Z", "Q", since=9000.0)
    _two_lock_cycle(reg, "aa", "A", "P", since=100.0)

    cycles = reg.deadlocks(now=NOW, threshold_minutes=0)
    assert len(cycles) >= 2, f"precondition: both cycles were detected, got {cycles}"

    first = {record.name for record in cycles[0]}
    assert first & {"zzlock", "zzheld"}, (
        f"deadlocks() must lead with the oldest cycle (the 'zz' cycle, whose edges are "
        f"9000s old) so remediation releases the older claim; it led with {sorted(first)} "
        f"whose edges are only 100s old. The cycles are returned in all_records() "
        f"filename order, which is unrelated to age."
    )


def test_control_a_single_old_cycle_is_still_detected() -> None:
    """Both cycles are real; only the ordering is wrong."""
    reg = LockRegistry("op")
    _two_lock_cycle(reg, "zz", "Z", "Q", since=9000.0)
    _two_lock_cycle(reg, "aa", "A", "P", since=100.0)
    cycles = reg.deadlocks(now=NOW, threshold_minutes=0)
    reported = {record.name for cycle in cycles for record in cycle}
    assert {"zzlock", "zzheld", "aalock", "aaheld"} <= reported
