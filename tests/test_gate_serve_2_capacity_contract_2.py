"""contract-2: ``restore`` adopts persisted targets without applying the bounds.

``CapacityBounds`` is documented as "the hard limits a target can never leave"
and the module insists they are "applied after every adjustment". Every
*adjustment* path in :meth:`CapacityController.tick` clamps, but ``restore`` is
not an adjustment — it copies whatever the previous supervisor's ``capacity.json``
held, verbatim.

That makes the floor/ceiling contract survive a supervisor restart only if
nothing changed in config. The realistic case is an operator lowering
``fleet_ops.serve`` lanes_ceiling, or a stale file from a box that used a much
higher ceiling: on the first tick the controller is holding an out-of-bounds
target, and that value is exactly what ``write_capacity`` publishes to the
dispatcher and gate, which read ``targets.max_lanes`` directly.

The failure is time-boxed only by luck — a SIGNAL_HOLD tick happens to clamp,
but SIGNAL_UNKNOWN deliberately does not (it sets the floor fields explicitly
while copying the rest of the previous targets verbatim), so the oversized
value can outlive the restart.
"""

from __future__ import annotations

from agent_fleet.serve.capacity import (
    CapacityBounds,
    CapacityController,
    CapacityPolicy,
    Watermarks,
    capacity_document,
)
from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.pressure import CpuPressure, PressureReading


def _reading(avg60: float = 0.0) -> PressureReading:
    return PressureReading(
        ok=True,
        error="",
        path="/fake/cgroup",
        cpu=CpuPressure(some_avg60=avg60, some_total_us=12345),
        memory_used_bytes=1_000_000,
        memory_max_bytes=10_000_000,
        memory_ratio=0.1,
    )


def _policy(bounds: CapacityBounds) -> CapacityPolicy:
    return CapacityPolicy(
        bounds=bounds,
        marks=Watermarks(low=10.0, high=25.0, memory_low=0.7, memory_high=0.85),
        starvation_ticks=3,
    )


def test_restored_targets_respect_a_lowered_ceiling() -> None:
    wide = CapacityBounds(lanes_floor=1, lanes_ceiling=50)
    first = CapacityController(_policy(wide), clock=FakeClock())
    for _ in range(60):
        first.tick(_reading(avg60=0.0), completed=1)
    assert first.targets.max_lanes == 50, "precondition: the wide ceiling was reached"

    document = capacity_document("op", first.targets, _reading(), clock=FakeClock())

    # The operator restarts serve having lowered the configured ceiling to 10.
    narrow = CapacityBounds(lanes_floor=1, lanes_ceiling=10)
    second = CapacityController(_policy(narrow), clock=FakeClock())
    second.restore(document)

    assert second.targets.max_lanes == narrow.clamp_lanes(50), (
        f"restore() adopted max_lanes={second.targets.max_lanes}, above the "
        f"configured hard ceiling of {narrow.lanes_ceiling}; every other "
        f"adjustment path clamps, so the resumed state must too"
    )
    assert second.targets.max_lanes <= narrow.lanes_ceiling


def test_restored_targets_respect_a_raised_floor() -> None:
    """The same gap on the other side of the interval: a stale *low* value."""
    previous = CapacityBounds(lanes_floor=1, lanes_ceiling=50)
    first = CapacityController(_policy(previous), clock=FakeClock())
    for _ in range(10):
        first.tick(_reading(avg60=99.0), completed=1)
    document = capacity_document("op", first.targets, _reading(), clock=FakeClock())
    assert first.targets.max_lanes == 1, "precondition: the old floor was in force"

    current = CapacityBounds(lanes_floor=3, lanes_ceiling=50)
    second = CapacityController(_policy(current), clock=FakeClock())
    second.restore(document)

    assert second.targets.max_lanes >= current.lanes_floor, (
        f"restore() adopted max_lanes={second.targets.max_lanes}, below the "
        f"configured hard floor of {current.lanes_floor}"
    )
