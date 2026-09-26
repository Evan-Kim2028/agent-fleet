"""AIMD capacity control: how many lanes and gates the fleet may run at once.

One rule, tuned three ways.

**The signal.** :mod:`agent_fleet.serve.pressure` — PSI and the memory ratio.
Never the load average (see that module for why the load average is actively
misleading under a cgroup quota).

**The response.** Additive increase, multiplicative decrease. Below the low
watermark, add ``step``; above the high watermark, multiply by ``decrease``.
Between the two, do nothing — that band is the hysteresis, and without it a
controller oscillating across a single threshold burns half its decisions
flipping direction and lands on neither the old nor the new capacity.

**The floors.** ``floor``/``ceiling`` are hard, from config, and are applied
after every adjustment rather than by clamping the inputs. A controller whose
ceiling is advisory is a controller that will exceed it during the one tick
where pressure spikes.

**Failure is not idleness.** If the pressure source is unreadable the
controller drops to the floor and records why. Ramping up because a file is
missing is how a capacity controller takes the machine down, so that path is
deliberately the opposite of the healthy one.

**Starvation guard.** Saturation plus zero completions for ``starvation_minutes``
means the fleet is spinning without shipping anything. More lanes will not
help, and the expensive thing already in flight — a gate — is what unblocks the
queue. So the guard collapses the lane target to its floor and sets
``gates_priority``, which is projected into the capacity file the dispatcher
and gate read.

The whole state is three integers plus a timestamp, so it serialises to the
capacity file and a restarting supervisor resumes mid-ramp instead of starting
over at the default.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from agent_fleet.serve.clock import SystemClock
from agent_fleet.serve.paths import capacity_path, write_json_atomic

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.serve.clock import Clock
    from agent_fleet.serve.pressure import PressureReading

#: The capacity file's contract. Bumping the version means every reader has to
#: be updated in the same change — the point of having it at all.
CAPACITY_SCHEMA = "agent-fleet.serve.capacity/1"

#: Signal classes the AIMD rule distinguishes.
SIGNAL_EASE = "ease"
SIGNAL_HOLD = "hold"
SIGNAL_PRESSURE = "pressure"
SIGNAL_UNKNOWN = "unknown"

#: How hard memory is allowed to press before it counts as pressure, as a
#: fraction of memory.max. Deliberately well below 1.0: OOM is not the thing to
#: react to, reclaim thrash is.
DEFAULT_MEMORY_HIGH_RATIO = 0.85
DEFAULT_MEMORY_LOW_RATIO = 0.70


@dataclass(frozen=True)
class CapacityBounds:
    """The hard limits a target can never leave. From config, never inferred."""

    lanes_floor: int = 1
    lanes_ceiling: int = 20
    gates_floor: int = 1
    gates_ceiling: int = 12
    tests_floor: int = 1
    tests_ceiling: int = 6
    typecheck_floor: int = 1
    typecheck_ceiling: int = 6

    def clamp_lanes(self, value: int) -> int:
        return max(self.lanes_floor, min(self.lanes_ceiling, value))

    def clamp_gates(self, value: int) -> int:
        return max(self.gates_floor, min(self.gates_ceiling, value))

    def clamp_tests(self, value: int) -> int:
        return max(self.tests_floor, min(self.tests_ceiling, value))

    def clamp_typecheck(self, value: int) -> int:
        return max(self.typecheck_floor, min(self.typecheck_ceiling, value))


@dataclass(frozen=True)
class Watermarks:
    """PSI ``some-avg60`` thresholds. Below low -> add; above high -> drop."""

    low: float = 10.0
    high: float = 25.0
    memory_low: float = DEFAULT_MEMORY_LOW_RATIO
    memory_high: float = DEFAULT_MEMORY_HIGH_RATIO

    def __post_init__(self) -> None:
        if self.low >= self.high:
            raise ValueError(f"low watermark {self.low} must be below high watermark {self.high}")
        if self.memory_low >= self.memory_high:
            raise ValueError(
                f"memory low watermark {self.memory_low} must be below {self.memory_high}"
            )


@dataclass(frozen=True)
class CapacityTargets:
    """What the fleet is allowed to do right now, and why."""

    max_lanes: int
    max_gates: int
    test_pool: int
    typecheck_pool: int
    #: The class the last AIMD decision came from; shown in ``serve status``.
    signal: str = SIGNAL_HOLD
    #: Human-readable justification for the current targets.
    reason: str = ""
    #: True when the starvation guard is steering toward finishing gates.
    gates_priority: bool = False
    #: False when the pressure source was unreadable and targets are at floor.
    degraded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_lanes": self.max_lanes,
            "max_gates": self.max_gates,
            "test_pool": self.test_pool,
            "typecheck_pool": self.typecheck_pool,
            "signal": self.signal,
            "reason": self.reason,
            "gates_priority": self.gates_priority,
            "degraded": self.degraded,
        }


def classify(
    reading: PressureReading,
    marks: Watermarks,
) -> tuple[str, str]:
    """Decide which regime the reading is in. Returns ``(signal, reason)``.

    Memory is weighed first: running out of memory kills work, while CPU stall
    merely slows it, so the more severe of the two decides.
    """
    if not reading.ok:
        return SIGNAL_UNKNOWN, reading.error or "pressure source unavailable"

    ratio = reading.memory_ratio
    if ratio is not None:
        if ratio >= marks.memory_high:
            return SIGNAL_PRESSURE, f"memory {ratio:.0%} of max >= {marks.memory_high:.0%}"
        if ratio <= marks.memory_low:
            return SIGNAL_EASE, f"memory {ratio:.0%} of max <= {marks.memory_low:.0%}"

    stall = reading.cpu.some_avg60
    if stall >= marks.high:
        return SIGNAL_PRESSURE, f"cpu some-avg60 {stall:.1f}% >= {marks.high:.1f}%"
    if stall <= marks.low:
        return SIGNAL_EASE, f"cpu some-avg60 {stall:.1f}% <= {marks.low:.1f}%"
    return SIGNAL_HOLD, f"cpu some-avg60 {stall:.1f}% inside [{marks.low:.1f}, {marks.high:.1f}]"


@dataclass(frozen=True)
class CapacityPolicy:
    """The tunable knobs, all of which come from ``fleet_ops.serve``."""

    bounds: CapacityBounds = CapacityBounds()
    marks: Watermarks = Watermarks()
    #: Additive increase, in lanes/gates, per tick at full headroom.
    step: int = 1
    #: Multiplicative decrease factor. 0.5 is a halving.
    decrease: float = 0.5
    #: Ticks that must show no completions before the starvation guard fires.
    starvation_ticks: int = 6


class CapacityController:
    """The AIMD state machine. Small enough to reason about in one sitting.

    The controller is pure with respect to its inputs: given the same pressure
    reading, the same tick sequence and the same completion signal, it produces
    the same targets. The clock is injected so a test can run an hour of
    simulated ticks instantly, and the state is serialisable so a restarting
    supervisor resumes its ramp rather than snapping back to the default.
    """

    def __init__(
        self,
        policy: CapacityPolicy | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.policy = policy or CapacityPolicy()
        self.clock = clock or SystemClock()
        self.targets = CapacityTargets(
            max_lanes=self.policy.bounds.clamp_lanes(self.policy.bounds.lanes_floor),
            max_gates=self.policy.bounds.clamp_gates(self.policy.bounds.gates_floor),
            test_pool=self.policy.bounds.clamp_tests(self.policy.bounds.tests_floor),
            typecheck_pool=self.policy.bounds.clamp_typecheck(self.policy.bounds.typecheck_floor),
        )
        #: Consecutive ticks with no completion. Reset by ``note_completion``.
        self.idle_ticks = 0
        #: Whether the last tick saw any completion at all.
        self.saturated = False
        self._degraded_ticks = 0

    # ------------------------------------------------------------ state carry-over

    def restore(self, payload: dict[str, Any]) -> None:
        """Adopt targets from a previous supervisor's capacity file."""
        targets = payload.get("targets")
        if isinstance(targets, dict):
            self.targets = CapacityTargets(
                max_lanes=int(targets.get("max_lanes", self.targets.max_lanes)),
                max_gates=int(targets.get("max_gates", self.targets.max_gates)),
                test_pool=int(targets.get("test_pool", self.targets.test_pool)),
                typecheck_pool=int(targets.get("typecheck_pool", self.targets.typecheck_pool)),
                signal=str(targets.get("signal", self.targets.signal)),
                reason=str(targets.get("reason", self.targets.reason)),
                gates_priority=bool(targets.get("gates_priority", False)),
                degraded=bool(targets.get("degraded", False)),
            )
        idle = payload.get("idle_ticks")
        if isinstance(idle, int):
            self.idle_ticks = idle
        saturated = payload.get("saturated")
        if isinstance(saturated, bool):
            self.saturated = saturated

    # -------------------------------------------------------------------- signals

    def note_completion(self, *, count: int = 1) -> None:
        """Record that *count* items finished this tick. Resets starvation."""
        if count > 0:
            self.idle_ticks = 0

    def _starving(self) -> bool:
        return self.idle_ticks >= max(1, self.policy.starvation_ticks)

    # ------------------------------------------------------------------ the rule

    def tick(self, reading: PressureReading, *, completed: int = 0) -> CapacityTargets:
        """One decision. Returns the targets now in force."""
        if completed > 0:
            self.note_completion(count=completed)
        else:
            self.idle_ticks += 1

        signal, reason = classify(reading, self.policy.marks)
        current = self.targets
        bounds = self.policy.bounds

        if signal == SIGNAL_UNKNOWN:
            # Unreadable pressure is not an idle machine. Fall to the floor and
            # stay there until a real reading arrives; ramping up here is how a
            # missing file becomes an outage.
            self._degraded_ticks += 1
            self.saturated = self.idle_ticks > 0
            self.targets = replace(
                current,
                max_lanes=bounds.clamp_lanes(bounds.lanes_floor),
                max_gates=bounds.clamp_gates(bounds.gates_floor),
                test_pool=bounds.clamp_tests(bounds.tests_floor),
                typecheck_pool=bounds.clamp_typecheck(bounds.typecheck_floor),
                signal=SIGNAL_UNKNOWN,
                reason=f"degraded: {reason}",
                degraded=True,
                gates_priority=False,
            )
            return self.targets

        self._degraded_ticks = 0
        self.saturated = signal == SIGNAL_PRESSURE

        if signal == SIGNAL_EASE:
            step = max(1, self.policy.step)
            lanes = bounds.clamp_lanes(current.max_lanes + step)
            gates = bounds.clamp_gates(current.max_gates + step)
            tests = bounds.clamp_tests(current.test_pool + step)
            typecheck = bounds.clamp_typecheck(current.typecheck_pool + step)
        elif signal == SIGNAL_PRESSURE:
            factor = min(1.0, max(0.0, self.policy.decrease))
            lanes = bounds.clamp_lanes(math.floor(current.max_lanes * factor))
            gates = bounds.clamp_gates(math.floor(current.max_gates * factor))
            tests = bounds.clamp_tests(math.floor(current.test_pool * factor))
            typecheck = bounds.clamp_typecheck(math.floor(current.typecheck_pool * factor))
        else:
            lanes = bounds.clamp_lanes(current.max_lanes)
            gates = bounds.clamp_gates(current.max_gates)
            tests = bounds.clamp_tests(current.test_pool)
            typecheck = bounds.clamp_typecheck(current.typecheck_pool)

        gates_priority = self._starving() and self.saturated
        if gates_priority:
            # Gates are what actually unblock a saturated queue; more lanes only
            # add work that cannot finish.
            lanes = bounds.clamp_lanes(bounds.lanes_floor)
            gates = bounds.clamp_gates(max(gates, bounds.gates_floor))
            reason = (
                f"{reason}; starved {self.idle_ticks} ticks at saturation — "
                f"holding lanes at floor and prioritising gates"
            )

        self.targets = CapacityTargets(
            max_lanes=lanes,
            max_gates=gates,
            test_pool=tests,
            typecheck_pool=typecheck,
            signal=signal,
            reason=reason,
            gates_priority=gates_priority,
            degraded=False,
        )
        return self.targets


def capacity_document(
    operator: str,
    targets: CapacityTargets,
    reading: PressureReading,
    *,
    clock: Clock | None = None,
    idle_ticks: int = 0,
    saturated: bool = False,
) -> dict[str, Any]:
    """The JSON document written to ``capacity.json``.

    This is the *only* channel between serve and the components it composes.
    The schema is documented in ``docs/FLEET-SERVE.md``; the fields are chosen
    so a component that wants to be correct cannot be: a dispatcher that reads
    ``targets.max_lanes`` and a gate that reads ``targets.max_gates`` both get a
    number, and ``pressure.ok`` tells a reader whether that number came from a
    real measurement.
    """
    the_clock = clock or SystemClock()
    return {
        "schema": CAPACITY_SCHEMA,
        "operator": operator,
        "updated_epoch": the_clock.time(),
        "targets": targets.to_dict(),
        "pressure": reading.to_dict(),
        "idle_ticks": idle_ticks,
        "saturated": saturated,
    }


def write_capacity(
    operator: str,
    targets: CapacityTargets,
    reading: PressureReading,
    *,
    clock: Clock | None = None,
    idle_ticks: int = 0,
    saturated: bool = False,
    path: Path | None = None,
) -> Path:
    """Publish the targets atomically. Readers never see a partial file."""
    target_path = path or capacity_path(operator)
    write_json_atomic(
        target_path,
        capacity_document(
            operator,
            targets,
            reading,
            clock=clock,
            idle_ticks=idle_ticks,
            saturated=saturated,
        ),
    )
    return target_path


def read_capacity(path: Path) -> dict[str, Any] | None:
    """Read a capacity file, or ``None`` when absent or unparseable."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return None
    return raw if isinstance(raw, dict) else None


__all__ = [
    "CAPACITY_SCHEMA",
    "DEFAULT_MEMORY_HIGH_RATIO",
    "DEFAULT_MEMORY_LOW_RATIO",
    "SIGNAL_EASE",
    "SIGNAL_HOLD",
    "SIGNAL_PRESSURE",
    "SIGNAL_UNKNOWN",
    "CapacityBounds",
    "CapacityController",
    "CapacityPolicy",
    "CapacityTargets",
    "Watermarks",
    "capacity_document",
    "classify",
    "read_capacity",
    "write_capacity",
]
