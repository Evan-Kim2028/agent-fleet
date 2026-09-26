"""correctness-4: a restored target is re-published verbatim, out of bounds.

This is the publication half of the same ``restore`` gap, and the half that
reaches production: the capacity file is documented as "the *only* channel
between serve and the components it composes", and a dispatcher that reads
``targets.max_lanes`` and a gate that reads ``targets.max_gates`` both just take
the number. So whatever ``restore`` adopts is what the fleet is told it may do
— the next tick's clamp is not a mitigation, it is a tick too late.

The scenario is a stale or edited ``capacity.json`` written under a much higher
configured ceiling, surviving a restart against a smaller one (an operator
lowering ``fleet_ops.serve``, or a box whose config was tightened between
restarts). ``CapacityBounds`` is documented as "the hard limits a target can
never leave"; the test asserts it across a ``write_capacity``/``read_capacity``
round trip, not just on the in-memory object.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from agent_fleet.serve.capacity import (
    CapacityBounds,
    CapacityController,
    write_capacity,
)
from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.pressure import CpuPressure, PressureReading

if TYPE_CHECKING:
    from pathlib import Path

HOSTILE = {
    "max_lanes": 9999,
    "max_gates": 999,
    "test_pool": 500,
    "typecheck_pool": 500,
}


def _reading() -> PressureReading:
    return PressureReading(
        ok=True,
        path="/fake/cgroup",
        cpu=CpuPressure(some_avg60=15.0, some_total_us=12345),
        memory_ratio=0.1,
    )


def test_restore_clamps_hostile_targets_to_the_configured_bounds() -> None:
    bounds = CapacityBounds()  # documented defaults: lanes<=20, gates<=12
    controller = CapacityController(clock=FakeClock())

    controller.restore({"targets": dict(HOSTILE)})

    assert controller.targets.max_lanes <= bounds.lanes_ceiling, (
        f"restore() adopted max_lanes={controller.targets.max_lanes}, above the "
        f"hard ceiling of {bounds.lanes_ceiling}"
    )
    assert controller.targets.max_gates <= bounds.gates_ceiling
    assert controller.targets.test_pool <= bounds.tests_ceiling
    assert controller.targets.typecheck_pool <= bounds.typecheck_ceiling


def test_out_of_bounds_targets_are_never_written_to_the_capacity_file(
    tmp_path: Path,
) -> None:
    """The published file is what the dispatcher and gate actually read."""
    bounds = CapacityBounds()
    controller = CapacityController(clock=FakeClock())
    controller.restore({"targets": dict(HOSTILE)})

    path = write_capacity(
        "op",
        controller.targets,
        _reading(),
        clock=FakeClock(),
        idle_ticks=controller.idle_ticks,
        saturated=controller.saturated,
        path=tmp_path / "capacity.json",
    )
    published = json.loads(path.read_text(encoding="utf-8"))["targets"]

    assert published["max_lanes"] <= bounds.lanes_ceiling, (
        f"capacity.json published max_lanes={published['max_lanes']} to the "
        f"dispatcher, {published['max_lanes'] / bounds.lanes_ceiling:.0f}x the "
        f"configured hard ceiling of {bounds.lanes_ceiling}"
    )
    assert published["max_gates"] <= bounds.gates_ceiling
    assert published["test_pool"] <= bounds.tests_ceiling
    assert published["typecheck_pool"] <= bounds.typecheck_ceiling


def test_restore_clamps_values_below_the_configured_floor() -> None:
    bounds = CapacityBounds(lanes_floor=3, gates_floor=2, tests_floor=2, typecheck_floor=2)
    from agent_fleet.serve.capacity import CapacityPolicy

    controller = CapacityController(CapacityPolicy(bounds=bounds), clock=FakeClock())
    controller.restore({"targets": {"max_lanes": 0, "max_gates": 0, "test_pool": 0}})

    assert controller.targets.max_lanes >= bounds.lanes_floor
    assert controller.targets.max_gates >= bounds.gates_floor
    assert controller.targets.test_pool >= bounds.tests_floor
