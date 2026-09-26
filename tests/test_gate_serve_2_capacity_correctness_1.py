"""correctness-1: the completion signal is a cumulative total, not a per-tick delta.

``ItemBoard.completed_this_tick`` sums *every* terminal transition in the log
with no lower time bound, so appending one STAGE_MERGED transition pins the
method's return value at 1 for the rest of the process's life — two hours, two
days, a supervisor restart later. Advancing the clock does not change it.

The downstream effect is the AIMD starvation guard. ``CapacityController.tick``
treats ``completed > 0`` as "the fleet shipped something this tick" and resets
``idle_ticks`` to zero, so a stale one-off merge is indistinguishable from live
throughput. With ``starvation_ticks=3`` and permanently saturated pressure, the
controller sits at ``idle_ticks == 0`` forever: ``gates_priority`` never becomes
true and ``max_lanes`` never collapses to the floor.

The existing unit test for the guard
(``tests/test_serve_capacity.py::test_starvation_guard_collapses_lanes_and_prioritises_gates``)
passes an explicit ``completed=0``, so it never exercises this path. This test
does, by feeding the controller the board's own value.
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

TWO_HOURS = 7200.0
TICKS = 6
STARVATION_TICKS = 3


def _saturated() -> PressureReading:
    return PressureReading(
        ok=True,
        path="/fake/cgroup",
        cpu=CpuPressure(some_avg60=99.0, some_total_us=12345),
        memory_ratio=0.1,
    )


def _policy() -> CapacityPolicy:
    return CapacityPolicy(
        bounds=CapacityBounds(lanes_ceiling=10, gates_ceiling=6),
        marks=Watermarks(low=10.0, high=25.0, memory_low=0.7, memory_high=0.85),
        starvation_ticks=STARVATION_TICKS,
    )


def test_completion_count_returns_to_zero_once_the_merge_is_old(tmp_path: Path) -> None:
    clock = FakeClock(start_time=1_000_000.0)
    board = ItemBoard(tmp_path / "items.jsonl", clock=clock)
    board.record("repo-a#7", STAGE_MERGED)

    assert board.completed_this_tick(now=clock.time()) == 1

    clock.advance(TWO_HOURS)
    later = board.completed_this_tick(now=clock.time())
    assert later == 0, (
        f"completed_this_tick() returned {later} two hours after the only merge; "
        f"the sole terminal transition is {TWO_HOURS / 3600:.0f}h old and no item "
        f"reached a terminal stage in the last tick"
    )


def test_starvation_counter_advances_when_the_fleet_ships_nothing(tmp_path: Path) -> None:
    clock = FakeClock(start_time=1_000_000.0)
    board = ItemBoard(tmp_path / "items.jsonl", clock=clock)
    board.record("repo-a#7", STAGE_MERGED)
    clock.advance(TWO_HOURS)

    controller = CapacityController(_policy(), clock=clock)
    for _ in range(TICKS):
        controller.tick(_saturated(), completed=board.completed_this_tick(now=clock.time()))

    assert controller.idle_ticks >= STARVATION_TICKS, (
        f"idle_ticks stayed at {controller.idle_ticks} after {TICKS} saturated ticks "
        f"with zero real completions, because a one-off merge from "
        f"{TWO_HOURS / 3600:.0f}h ago is still being reported every tick"
    )
    assert controller.targets.gates_priority is True
    assert controller.targets.max_lanes == _policy().bounds.lanes_floor
