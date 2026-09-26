"""all-2: a serial chain named by ref is not a dependency deadlock.

Claim under test
----------------
``blocked_lanes`` seeds its fixpoint with ``released | settled`` but only ever
adds a lane *name* to ``settled``::

    released = terminal_refs(state)      # yields REFS and lane names
    settled: set[str] = set()
    ...
    if dependency_satisfied(lane, released | settled, known):
        settled.add(lane.lane)           # name only

``dependency_satisfied`` resolves ``depends_on`` against the released set, and
``depends_on`` is written in either spelling (a triage ref like ``R-1234`` or a
bare lane name) -- that is the whole reason ``known_dependency_keys`` exists. So
a dependency named by its *ref* never matches the name-only ``settled`` set, and
every lane past the first link of a ref chain is reported as permanently blocked
on a queue with no cycle anywhere in it.

The damage is not cosmetic, because the caller acts on it destructively::

    stuck = blocked_lanes(current, ...)
    if stuck and _complete(current):
        for name in stuck:
            current = _finish(current, name, "dependency_deadlock", ...)

``_finish`` writes ``DISPATCH_DONE``, which is terminal and counts an error. A
perfectly valid serial queue is therefore killed as a permanent dependency
deadlock whenever the pool is momentarily empty and every remaining lane is
waiting on the next link of the chain -- a restart whose recorded pid is gone,
or a tick where the only in-flight work was the previous link.

Repro: ``A`` done (ref ``R-0``), ``B`` queued depending on ``R-0``, ``C`` queued
depending on ``R-1``. ``blocked_lanes`` returned ``['C']`` while
``_dispatchable_lanes`` returned ``['B']`` -- the same state, two functions,
opposite answers, and no cycle in the graph.

The fix releases both spellings, mirroring ``known_dependency_keys``. The
assertions below pin the false positive *and* the detection, because a fix that
simply widened the fixpoint would also silence real cycles -- the failure mode
that matters more, since a missed cycle is a permanent hang.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agent_fleet.fleet_ops.dispatch import (
    DISPATCH_DONE,
    DISPATCH_QUEUED,
    DispatchItem,
    DispatchLane,
    DispatchState,
    _dispatchable_lanes,
    blocked_lanes,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.usefixtures("_isolated_home")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


def _lane(name: str, ref: str, state: str, depends_on: tuple[str, ...] = ()) -> DispatchLane:
    return DispatchLane(
        lane=name,
        state=state,
        item=DispatchItem(
            lane=name,
            repo="acme",
            task=f"t{name}",
            ref=ref,
            depends_on=depends_on,
        ),
    )


def _state(*lanes: DispatchLane) -> DispatchState:
    return DispatchState(operator="documents-0e", lanes={lane.lane: lane for lane in lanes})


def test_a_serial_chain_named_by_ref_is_not_reported_as_blocked() -> None:
    """The claim's repro: no cycle, one link resolvable at a time.

    ``plan_tick`` launches ``B`` on this state, so ``blocked_lanes`` claiming
    ``C`` is permanently blocked contradicts the planner on the same input.
    """
    state = _state(
        _lane("A", "R-0", DISPATCH_DONE),
        _lane("B", "R-1", DISPATCH_QUEUED, ("R-0",)),
        _lane("C", "R-2", DISPATCH_QUEUED, ("R-1",)),
    )

    dispatchable = [lane.lane for lane in _dispatchable_lanes(state)]

    assert dispatchable == ["B"], f"setup changed: dispatchable={dispatchable}"
    assert blocked_lanes(state) == [], (
        f"blocked_lanes reported {blocked_lanes(state)} on a plain serial chain with no "
        "cycle: a dependency named by its ref (R-1) never matched the name-only "
        "`settled` set, so the lane past the first link looked permanently blocked"
    )


def test_a_longer_ref_chain_is_not_reported_as_blocked() -> None:
    """Every link past the first is affected, not just the second."""
    state = _state(
        _lane("A", "R-0", DISPATCH_DONE),
        _lane("B", "R-1", DISPATCH_QUEUED, ("R-0",)),
        _lane("C", "R-2", DISPATCH_QUEUED, ("R-1",)),
        _lane("D", "R-3", DISPATCH_QUEUED, ("R-2",)),
        _lane("E", "R-4", DISPATCH_QUEUED, ("R-3",)),
    )

    assert blocked_lanes(state) == [], (
        f"blocked_lanes reported {blocked_lanes(state)}; a five-link serial chain "
        "written entirely in refs contains no cycle and nothing in it can ever run"
    )


def test_both_spellings_and_a_mix_of_them_resolve() -> None:
    """``depends_on`` is written both ways and a queue may mix them."""
    by_name = _state(
        _lane("A", "R-0", DISPATCH_DONE),
        _lane("B", "R-1", DISPATCH_QUEUED, ("A",)),
        _lane("C", "R-2", DISPATCH_QUEUED, ("B",)),
    )
    mixed = _state(
        _lane("A", "R-0", DISPATCH_DONE),
        _lane("B", "R-1", DISPATCH_QUEUED, ("R-0",)),
        _lane("C", "R-2", DISPATCH_QUEUED, ("B",)),
        _lane("D", "R-3", DISPATCH_QUEUED, ("R-2",)),
    )

    assert blocked_lanes(by_name) == [], f"name-spelled: {blocked_lanes(by_name)}"
    assert blocked_lanes(mixed) == [], f"mixed-spelled: {blocked_lanes(mixed)}"


def test_a_real_ref_cycle_is_still_reported_as_blocked() -> None:
    """Widening the fixpoint must not silence genuine cycles.

    A missed cycle is the worse failure of the two: the false positive is a
    valid queue killed, but a false negative is a queue that hangs forever,
    which is the silent-hang this function exists to prevent.
    """
    state = _state(
        _lane("A", "R-0", DISPATCH_QUEUED, ("R-2",)),
        _lane("B", "R-1", DISPATCH_QUEUED, ("R-0",)),
        _lane("C", "R-2", DISPATCH_QUEUED, ("R-1",)),
    )

    assert sorted(blocked_lanes(state)) == ["A", "B", "C"], (
        f"a real cycle named by ref reported {blocked_lanes(state)}; the loop is "
        "permanently unsatisfiable and no run order launches any of it"
    )


def test_a_real_cycle_and_the_lane_stuck_behind_it_are_still_reported() -> None:
    """Including the lane waiting on a member of the cycle."""
    state = _state(
        _lane("A", "R-0", DISPATCH_QUEUED, ("R-1",)),
        _lane("B", "R-1", DISPATCH_QUEUED, ("R-0",)),
        _lane("C", "R-2", DISPATCH_QUEUED, ("R-0",)),
    )

    assert sorted(blocked_lanes(state)) == ["A", "B", "C"], (
        f"cycle plus dependent lane reported {blocked_lanes(state)}; C waits on a "
        "lane inside the cycle, so it can never launch either"
    )


def test_a_self_cycle_and_a_name_spelled_cycle_are_still_reported() -> None:
    """The degenerate and the bare-name forms of the same thing."""
    self_ref = _state(_lane("A", "R-0", DISPATCH_QUEUED, ("R-0",)))
    by_name = _state(
        _lane("A", "R-0", DISPATCH_QUEUED, ("B",)),
        _lane("B", "R-1", DISPATCH_QUEUED, ("A",)),
    )

    assert blocked_lanes(self_ref) == ["A"], f"self-cycle: {blocked_lanes(self_ref)}"
    assert sorted(blocked_lanes(by_name)) == ["A", "B"], (
        f"name-spelled cycle: {blocked_lanes(by_name)}"
    )
