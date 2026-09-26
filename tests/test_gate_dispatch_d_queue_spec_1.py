"""spec_1: the throttle-abandon condition is inverted.

Claim under test
----------------
The idle branch of ``run_dispatch`` is::

    if _throttled(psi) and not _has_queued_work(current):
        throttled_ticks = 0
    else:
        throttled_ticks += 1
    if throttled_ticks > throttle_max_ticks:
        ... finish the dispatchable lanes as throttle_abandoned; break

The counter is reset in the one state where abandonment is *impossible* (the box
IS throttled, so the run is legitimately waiting), and incremented in every other
state -- including the one where the machine is idle and a slot is legitimately
held.  The predicate for *incrementing* is therefore the exact complement of the
one for *resetting*, and the abandonment branch fires on states that are not
throttles at all.

The claimed repro: one lane in ``DISPATCH_GATING`` whose recorded identity is
the test process itself (a genuinely live gate that never exits), an idle PSI
reader, and ``throttle_max_ticks=3``.  The correct behaviour is to keep waiting
for the live gate.  The claimed behaviour is to log "still throttled after 4
ticks", break, and return ``exit_code() == 0`` with 0 errors and 0 escalated --
a green run that never collected the gate verdict it was waiting on.  The
abandonment path must also require that nothing occupies a slot.

The test asserts the contract: waiting on a live child is never reported as
success, and never counted as throttling on an idle box.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from agent_fleet.fleet_ops import pressure
from agent_fleet.fleet_ops.dispatch import (
    DISPATCH_GATING,
    DISPATCH_PR,
    DISPATCH_QUEUED,
    THROTTLE_ABANDONED,
    DispatchItem,
    DispatchLane,
    DispatchState,
    LaunchLane,
    plan_tick,
    run_dispatch,
)

#: An *idle* box: 1.0% CPU pressure against the 25.0% ceiling.
IDLE = pressure.Throttle(some_avg10=1.0, path=Path("/fake/cpu.pressure"), available=True)

THROTTLE_MAX_TICKS = 3


class _LiveProc:
    """A gate child that is still running: ``poll()`` returns None, forever."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode = None

    def poll(self) -> int | None:
        return None


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


def _run(tmp_path: Path) -> tuple[Any, int]:
    """A dispatch waiting on one live gate, on an idle box. Returns run, sleeps."""
    queue = tmp_path / "q.jsonl"
    queue.write_text(
        "\n".join(json.dumps({"lane": f"L{i}", "repo": "acme", "task": "t"}) for i in range(4)),
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    out_root = tmp_path / "out"

    lanes: dict[str, DispatchLane] = {
        "g0": DispatchLane(
            lane="g0",
            item=DispatchItem.from_dict({"lane": "g0", "repo": "acme", "task": "t"}),
            state=DISPATCH_GATING,
            pr=100,
            # A genuinely live gate: this very process, never reaped.
            gate_pid=os.getpid(),
            gate_pgid=os.getpgrp(),
            gate_starttime=None,
            status_file=str(out_root / "lanes" / "g0.status"),
        )
    }
    # max_lanes=1, so the single gate already fills the lane cap and L1..L3 stay
    # queued: the abandonment branch has real dispatchable work to throw away.
    for i in (1, 2, 3):
        lanes[f"L{i}"] = DispatchLane(
            lane=f"L{i}",
            item=DispatchItem.from_dict({"lane": f"L{i}", "repo": "acme", "task": "t"}),
            state=DISPATCH_QUEUED,
        )
    state = DispatchState(
        operator="documents-0e",
        queue_path=str(queue),
        lanes=lanes,
        procs={"g0": _LiveProc(7000)},
    )

    sleeps = 0

    def _sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps > 2 * THROTTLE_MAX_TICKS + 2:
            raise AssertionError(f"still waiting after {sleeps} ticks; it never abandons")

    summary = run_dispatch(
        operator="documents-0e",
        queue_path=queue,
        repos={"acme": str(repo)},
        max_lanes=1,
        max_gates=2,
        state=state,
        spawn=lambda *_a, **_k: _LiveProc(7100),
        psi_reader=lambda: IDLE,
        sleep=_sleep,
        throttle_max_ticks=THROTTLE_MAX_TICKS,
        run_dir=out_root,
    )
    return summary, sleeps


def test_waiting_on_a_live_gate_is_never_reported_as_a_green_run(tmp_path: Path) -> None:
    """A dispatcher that exits 0 while a gate it launched is unreaped lies.

    The gate is live for the whole run, so the loop must keep waiting. Returning
    ``exit_code() == 0`` with no verdict collected is the silent-drop failure
    this module documents, and it is what the inverted condition produces.
    """
    summary, sleeps = _run(tmp_path)
    final = summary.state
    assert final is not None
    gate = final.lanes["g0"]

    assert gate.state == DISPATCH_GATING and gate.reason is None, (
        f"lane g0 was resolved to state={gate.state!r} reason={gate.reason!r} after "
        f"{sleeps} ticks while its gate process was still alive; the run gave up on "
        "a gate it was waiting for"
    )
    assert summary.exit_code() == 0, (
        f"a run that abandoned a live gate after {sleeps} ticks reported "
        f"exit_code={summary.exit_code()} (summary={summary.to_dict()}) -- success for "
        "a gate verdict that was never collected"
    )


def test_an_idle_box_is_not_counted_as_throttled(tmp_path: Path) -> None:
    """``some_avg10=1.0`` against a 25.0 ceiling is not CPU pressure.

    No lane may be finished as ``throttle_abandoned`` on this reading: the
    abandonment branch is for a box that refused to free up, not one that was
    never busy.
    """
    summary, sleeps = _run(tmp_path)
    final = summary.state
    assert final is not None
    abandoned = sorted(
        lane.lane for lane in final.lanes.values() if lane.reason == THROTTLE_ABANDONED
    )

    assert not abandoned, (
        f"lanes {abandoned} were finished as {THROTTLE_ABANDONED!r} after {sleeps} "
        f"ticks with throttle_max_ticks={THROTTLE_MAX_TICKS} on an idle box "
        f"(psi throttled={pressure.throttled(IDLE)!r}, some_avg10=1.0 vs ceiling "
        f"{pressure.DEFAULT_PSI_AVG10_MAX}); the counter is incremented in the exact "
        "complement of the state that should reset it"
    )
    assert all(
        lane.state in (DISPATCH_GATING, DISPATCH_PR, DISPATCH_QUEUED)
        for lane in final.lanes.values()
    ), f"a lane went terminal without a verdict: {[lane.state for lane in final.lanes.values()]}"


def test_the_throttle_still_holds_back_a_launch_on_a_saturated_box() -> None:
    """Control: the throttle's correct half, so a fix cannot just delete it.

    The claim is that the *condition* is inverted, not that throttling is
    unwanted -- so a saturated reading must still stop ``plan_tick`` from
    launching anything.
    """
    saturated = pressure.Throttle(some_avg10=90.0, path=Path("/fake/cpu.pressure"), available=True)
    state = DispatchState(
        operator="documents-0e",
        lanes={
            f"L{i}": DispatchLane(
                lane=f"L{i}",
                item=DispatchItem.from_dict({"lane": f"L{i}", "repo": "acme", "task": "t"}),
                state=DISPATCH_QUEUED,
            )
            for i in range(3)
        },
    )

    assert not [a for a in plan_tick(state, psi=saturated) if isinstance(a, LaunchLane)], (
        "a saturated box must still launch nothing"
    )
    assert [a for a in plan_tick(state, psi=IDLE) if isinstance(a, LaunchLane)], (
        "an idle box must launch the queued lanes"
    )
