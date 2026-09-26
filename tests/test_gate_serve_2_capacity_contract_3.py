"""contract-3: the cgroup v1 branch computes a memory signal and then throws it away.

On a v1 host ``read_pressure`` cannot obtain PSI — v1 genuinely has no
per-cgroup ``cpu.pressure`` — so it sets ``ok=False``. That part is the module's
deliberate fail-closed policy and is correct on its own: "unreadable means
unknown", so the controller drops to the floor rather than ramping up.

The defect is that the same branch *does* read a perfectly good memory ratio
and returns it in ``memory_ratio``, then ``classify`` never looks at it, because
``classify`` returns ``SIGNAL_UNKNOWN`` for any ``ok=False`` reading before it
reaches the memory comparison. So a v1 box that is 95% full of its memory
ceiling is classified exactly like a v1 box whose cgroup is missing: unknown,
degraded, pinned to the lane floor, ``gates_priority`` force-disabled, forever
— with a real, correct measurement sitting unused in the payload.

The module documents the v1 branch as having a working memory signal ("Only the
memory ratio is available"), and the memory path itself is explicitly about
reclaim thrash rather than OOM (DEFAULT_MEMORY_HIGH_RATIO = 0.85), which is
precisely the condition v1 hosts cannot report at all today.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_fleet.serve.capacity import (
    SIGNAL_EASE,
    SIGNAL_PRESSURE,
    CapacityBounds,
    CapacityController,
    CapacityPolicy,
    Watermarks,
    classify,
)
from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.pressure import CpuPressure, PressureReading, read_pressure

if TYPE_CHECKING:
    from pathlib import Path


def _v1_tree(root: Path, *, used: int, limit: int) -> None:
    (root / "memory").mkdir(parents=True)
    (root / "cpu,cpuacct").mkdir(parents=True)
    cg = root / "agents.slice"
    cg.mkdir()
    (cg / "memory.usage_in_bytes").write_text(str(used), encoding="utf-8")
    (cg / "memory.limit_in_bytes").write_text(str(limit), encoding="utf-8")


def _healthy_reading() -> PressureReading:
    return PressureReading(
        ok=True,
        path="/fake/cgroup",
        cpu=CpuPressure(some_avg60=0.0, some_total_us=1),
        memory_ratio=0.1,
    )


def test_v1_memory_at_95_percent_classifies_as_pressure_not_unknown(tmp_path: Path) -> None:
    _v1_tree(tmp_path, used=9500, limit=10000)
    reading = read_pressure("agents.slice", root=tmp_path)

    assert reading.hierarchy == "v1"
    assert reading.memory_ratio is not None and reading.memory_ratio > 0.85, (
        "precondition: the v1 branch is expected to compute a usable memory ratio"
    )

    signal, reason = classify(reading, Watermarks())
    assert signal != SIGNAL_EASE, (
        f"classify reported {signal!r} ({reason!r}) for a v1 reading that is "
        f"{reading.memory_ratio:.0%} of its memory ceiling — above the 0.85 "
        f"high watermark; an available measurement must not be discarded"
    )
    assert signal == SIGNAL_PRESSURE, (
        f"classify reported {signal!r} ({reason!r}); a v1 cgroup 95% full is "
        f"memory pressure, and the module's own comment says only the memory "
        f"ratio is available on v1"
    )


def test_v1_memory_pressure_drives_the_controller_down_not_to_degraded_unknown(
    tmp_path: Path,
) -> None:
    _v1_tree(tmp_path, used=9500, limit=10000)
    reading = read_pressure("agents.slice", root=tmp_path)

    controller = CapacityController(
        CapacityPolicy(bounds=CapacityBounds(lanes_ceiling=10)),
        clock=FakeClock(),
    )
    for _ in range(5):
        controller.tick(_healthy_reading(), completed=1)
    assert controller.targets.max_lanes > 1, "precondition: the fleet was ramped up"

    targets = controller.tick(reading)
    assert targets.degraded is False, (
        "a v1 cgroup with a readable memory ratio is not an unreadable source; "
        f"got signal={targets.signal!r} degraded=True reason={targets.reason!r}"
    )
    assert targets.signal == SIGNAL_PRESSURE
    assert targets.max_lanes < 10
