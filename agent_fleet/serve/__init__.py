"""``fleet serve`` — one supervisor for the whole pipeline.

The bash fleet this replaces was six long-running shell loops, a Python
dispatcher, a 262-line gate, and a watchdog that scanned ``ps`` for engine
names. It worked, and it required an operator to be present: someone had to
notice a lane had stopped, notice a merge had wedged on a lock, notice an
``automerge.hold`` had grown, and restart the pieces. Every one of those
"notices" is a rule in :mod:`agent_fleet.serve.watchdog` here.

The shape is deliberate. Serve **supervises**; it does not reimplement. The
dispatcher, the merge executor and the janitor are separate programs spawned
from configurable command templates, so serve works with whatever those
components happen to be — the in-repo ``fleet dispatch`` and ``fleet merge
run``, or the bash drivers they replace — and the components keep their own
logic instead of having it forked into a supervisor that would then have to be
kept in sync with them.

What serve owns is everything *around* them, and owns it without a babysitter:

* :mod:`~agent_fleet.serve.supervisor` — restart, backoff, crash-loop
  detection, and re-attach on its own restart so a second supervisor never
  double-starts a component;
* :mod:`~agent_fleet.serve.capacity` — AIMD sizing from cgroup pressure, never
  from the load average, published as a documented file the components read;
* :mod:`~agent_fleet.serve.watchdog` — five detectors with bounded,
  fleet-owned remediations;
* :mod:`~agent_fleet.serve.escalate` — reason-class routing so a broken test
  runner is retried and a fence is never retried;
* :mod:`~agent_fleet.serve.status` — one screen, honest about what it could not
  measure.

The single rule everything else serves: **remediate only what the fleet owns.**
Every termination goes through an exact pid plus a start-time fingerprint
recorded when serve spawned the process, because this machine runs other agents
whose work must not be mistaken for a stuck lane.
"""

from agent_fleet.serve.capacity import (
    CAPACITY_SCHEMA,
    CapacityBounds,
    CapacityController,
    CapacityPolicy,
    CapacityTargets,
    Watermarks,
    read_capacity,
    write_capacity,
)
from agent_fleet.serve.clock import Clock, FakeClock, SystemClock
from agent_fleet.serve.config import (
    ComponentSpec,
    ServeConfig,
    ServeConfigError,
    WatchdogConfig,
    load_serve_config,
)
from agent_fleet.serve.escalate import (
    Decision,
    EscalationRouter,
    classify_reason,
    read_decisions,
)
from agent_fleet.serve.events import alert, emit_serve_event, read_serve_events
from agent_fleet.serve.items import STAGES, Item, ItemBoard, Transition
from agent_fleet.serve.locks import LockRecord, LockRegistry
from agent_fleet.serve.pressure import CpuPressure, PressureReading, read_pressure
from agent_fleet.serve.procs import ProcIdentity, pid_alive, terminate
from agent_fleet.serve.serve import ServeLoop
from agent_fleet.serve.status import render_status, status_snapshot
from agent_fleet.serve.supervisor import ChildState, Supervisor
from agent_fleet.serve.watchdog import Watchdog, WatchdogReport

__all__ = [
    "CAPACITY_SCHEMA",
    "STAGES",
    "CapacityBounds",
    "CapacityController",
    "CapacityPolicy",
    "CapacityTargets",
    "ChildState",
    "Clock",
    "ComponentSpec",
    "CpuPressure",
    "Decision",
    "EscalationRouter",
    "FakeClock",
    "Item",
    "ItemBoard",
    "LockRecord",
    "LockRegistry",
    "PressureReading",
    "ProcIdentity",
    "ServeConfig",
    "ServeConfigError",
    "ServeLoop",
    "Supervisor",
    "SystemClock",
    "Transition",
    "Watchdog",
    "WatchdogConfig",
    "WatchdogReport",
    "Watermarks",
    "alert",
    "classify_reason",
    "emit_serve_event",
    "load_serve_config",
    "pid_alive",
    "read_capacity",
    "read_decisions",
    "read_pressure",
    "read_serve_events",
    "render_status",
    "status_snapshot",
    "terminate",
    "write_capacity",
]
