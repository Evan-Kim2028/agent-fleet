"""contract-1: the AIMD starvation guard can never fire off a real ItemBoard.

``ItemBoard.completed_this_tick`` is documented as counting "how many items
reached a terminal stage *since the last tick*", and it is the sole feed for
``CapacityController``'s starvation guard. The implementation's comprehension
applies only an *upper* time bound (``t.epoch <= the_now``) and no lower one, so
it counts every terminal transition ever appended to the board.

The consequence is not a rounding error. The controller's starvation guard
advances ``idle_ticks`` only on a tick where ``completed == 0``; once a single
item has ever merged, ``completed_this_tick`` is ``>= 1`` on every subsequent
call, forever. ``idle_ticks`` is pinned at 0, ``_starving()`` never becomes true,
and ``gates_priority`` — the flag the module names as "what unblocks a wedged
fleet" — never turns on again for the life of the process.

This test drives the real board and the real controller together: one merged
item, then twenty saturated ticks during which the fleet ships nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_fleet.serve.capacity import (
    CapacityBounds,
    CapacityController,
    CapacityPolicy,
    Watermarks,
)
from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.items import STAGE_MERGED, ItemBoard
from agent_fleet.serve.pressure import CpuPressure, PressureReading

if TYPE_CHECKING:
    from pathlib import Path

SATURATED_TICKS = 20
TICK_SECONDS = 10.0


def _saturated_reading() -> PressureReading:
    """A healthy, readable cgroup whose CPU is stalled far above the high mark."""
    return PressureReading(
        ok=True,
        error="",
        path="/fake/cgroup",
        cpu=CpuPressure(some_avg60=99.0, some_total_us=12345),
        memory_used_bytes=1_000_000,
        memory_max_bytes=10_000_000,
        memory_ratio=0.1,
    )


def _policy() -> CapacityPolicy:
    return CapacityPolicy(
        bounds=CapacityBounds(
            lanes_floor=1,
            lanes_ceiling=10,
            gates_floor=1,
            gates_ceiling=6,
            tests_floor=1,
            tests_ceiling=4,
            typecheck_floor=1,
            typecheck_ceiling=4,
        ),
        marks=Watermarks(low=10.0, high=25.0, memory_low=0.7, memory_high=0.85),
        step=1,
        decrease=0.5,
        starvation_ticks=3,
    )


def test_starvation_guard_fires_after_one_merge_stops_shipping(tmp_path: Path) -> None:
    clock = FakeClock(start_time=1000.0)
    board = ItemBoard(tmp_path / "items.jsonl", clock=clock)
    board.record("repo-a#1", STAGE_MERGED)

    controller = CapacityController(_policy(), clock=clock)

    fired_at: int | None = None
    for tick in range(1, SATURATED_TICKS + 1):
        clock.advance(TICK_SECONDS)
        # The fleet genuinely ships nothing from here on: no further record().
        targets = controller.tick(
            _saturated_reading(), completed=board.completed_this_tick(now=clock.time())
        )
        if targets.gates_priority and fired_at is None:
            fired_at = tick

    assert fired_at is not None, (
        "the starvation guard never fired: completed_this_tick keeps reporting the "
        f"one-off merge as a fresh completion, so idle_ticks stayed at "
        f"{controller.idle_ticks} (needs >={_policy().starvation_ticks}) across "
        f"{SATURATED_TICKS} saturated ticks with zero real completions"
    )
    assert controller.targets.gates_priority is True
    assert controller.targets.max_lanes == _policy().bounds.lanes_floor
