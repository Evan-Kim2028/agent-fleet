"""Durable queue dispatch: run triaged items as lanes, then gate what shipped.

This replaces the per-operator ``dispatch.py`` shell drivers. The shell version
was a ~70-line loop whose state lived only in its own memory, which is how six
separate failures reached production. Each one is now structurally impossible,
and each has a test named after it:

1. **A finished lane missing from the queue crashed the loop.** The shell driver
   resolved a finished lane back to its queue item with ``next(q for q in queue
   if q["lane"] == lane)``, which raised ``StopIteration`` — and ``StopIteration``
   inside a generator context is a ``RuntimeError`` — taking the whole dispatcher
   down. Every finished lane after that point was never gated. Here the queue is
   a :class:`dict`, a lane with no item is *recorded* as ``unknown_item`` rather
   than looked up, and every action is individually guarded.

2. **Two operators shared one log and adopted each other's lanes.** They wrote
   the same ``events.log`` and detected liveness with ``"--lane X " in ps -eo
   args``, so operator A would wait on — and later gate — operator B's process.
   Here the durable state is namespaced by operator
   (:func:`dispatch_state_path`), events go to the registry's shared stream
   *tagged* with their operator, and liveness is a recorded ``(pid, starttime)``
   fingerprint. Nothing is ever found by matching a command line.

3. **Restarting relaunched lanes that had already run.** The shell version held
   its state in local variables, so a restart began from an empty slate. Here
   state is durable (:func:`load_state`/:func:`save_state`) and a lane is only
   ever launched when it has no terminal state *and* no live recorded process.

4. **Eighteen gates released at once drove the box to load 200.** The shell
   version's gate cap was enforced by *waiting in the launch loop*, which is
   where the one-tick overshoot that produced the 18 came from. Here
   :func:`plan_tick` counts the gates it has already decided to launch in the
   same tick, so the cap is exact.

5. **Throttling on ``loadavg`` blocked every launch for forty minutes.** This
   box runs agents under a cgroup CPU quota, so a throttled task is still
   *running* as far as the load average is concerned: load reads high when the
   machine is idle-but-quota-bound. The throttle is CPU PSI
   (:mod:`agent_fleet.fleet_ops.pressure`) and fails open. ``os.getloadavg`` is
   not called anywhere in this module.

The scheduling decision is a **pure function**, :func:`plan_tick`, over an
immutable :class:`DispatchState`. :func:`run_dispatch` only executes what
``plan_tick`` returns. That is what makes the five failures above testable
without spawning anything: every test drives ``plan_tick`` with injected state.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_fleet.fleet_ops import pressure, registry
from agent_fleet.fleet_ops.config import expand_template
from agent_fleet.fleet_ops.registry import (
    append_event,
    lanes_dir,
    local_hhmmss,
    process_starttime,
)
from agent_fleet.fleet_ops.statusfile import APPROVED_TOKEN, ESCALATION_TOKEN

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from typing import IO, Protocol

    #: A spawned child: pid, and a ``poll()`` returning its exit code (or None
    #: while it runs). A protocol rather than ``Any`` so the fake procs in the
    #: tests and the real ``Popen`` are checked against the same shape.
    class SpawnedProc(Protocol):
        pid: int

        def poll(self) -> int | None: ...


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- dispatch states

#: Dispatch-level lane states. Deliberately distinct from the lane registry's
#: ``STATE_*``: the registry describes what ``lane run`` is doing to a PR, this
#: describes what the *dispatcher* is doing to a queue item.
DISPATCH_QUEUED = "queued"
DISPATCH_RUNNING = "running"
DISPATCH_PR = "pr"
DISPATCH_GATING = "gating"
DISPATCH_DONE = "done"
#: Attempted and could not be started (bad repo path, unparseable command,
#: spawn failure). Terminal, so the dispatcher reports it instead of retrying.
DISPATCH_FAILED = "failed"

DISPATCH_STATES = frozenset(
    {
        DISPATCH_QUEUED,
        DISPATCH_RUNNING,
        DISPATCH_PR,
        DISPATCH_GATING,
        DISPATCH_DONE,
        DISPATCH_FAILED,
    }
)

#: States from which no further automatic work happens. ``done`` is the
#: completed outcome; ``failed`` is "attempted and could not be started", and is
#: terminal for the same reason — an operator has to look at it, and a
#: dispatcher that retries forever turns one broken path into a launch loop.
DISPATCH_TERMINAL = frozenset({DISPATCH_DONE, DISPATCH_FAILED})

#: A lane occupying one of the ``--max-lanes`` concurrency slots.
LANE_SLOT_STATES = frozenset({DISPATCH_RUNNING, DISPATCH_PR, DISPATCH_GATING})

#: A lane occupying one of the ``--max-gates`` gate slots.
GATE_SLOT_STATES = frozenset({DISPATCH_GATING})

#: Default concurrency. Deliberately far below the shell driver's 20 lanes /
#: 10 gates: the box sustained a fine swarm, not a stampede.
DEFAULT_MAX_LANES = 8
DEFAULT_MAX_GATES = 4

#: How often the dispatch loop re-evaluates.
DEFAULT_TICK_SECONDS = 20.0

#: Status of a lane whose queue item could not be found. The shell driver's
#: ``StopIteration``.
UNKNOWN_ITEM = "unknown_item"

DEFAULT_OWNER = "Evan-Kim2028"


# ---------------------------------------------------------------- queue items


@dataclass(frozen=True)
class DispatchItem:
    """One triaged queue item, as read from the JSONL queue.

    Only ``lane``, ``repo`` and ``task`` are required. Everything else is triage
    metadata that is carried into the generated task file so the implementer sees
    what triage already established, and ``depends_on``/``cluster`` which drive
    scheduling.
    """

    lane: str
    repo: str
    task: str
    ref: str = ""
    area: str = ""
    size: str = ""
    cluster: str = ""
    evidence: str = ""
    files: tuple[str, ...] = ()
    dbt_models: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()

    @property
    def dependency_refs(self) -> tuple[str, ...]:
        """``depends_on`` entries that are not also bare lane names.

        The shell driver accepted both spellings — ``depends_on: ["C0"]`` naming
        a *ref* and ``depends_on: ["lane-x"]`` naming a lane — and so does this.
        """
        return tuple(self.depends_on)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "repo": self.repo,
            "ref": self.ref,
            "task": self.task,
            "area": self.area,
            "size": self.size,
            "cluster": self.cluster,
            "evidence": self.evidence,
            "files": list(self.files),
            "dbt_models": list(self.dbt_models),
            "depends_on": list(self.depends_on),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DispatchItem:
        if not isinstance(raw, dict):
            raise ValueError(f"queue item must be a JSON object, got {type(raw).__name__}")
        lane = str(raw.get("lane") or "").strip()
        if not lane:
            raise ValueError("queue item has no 'lane'")
        repo = str(raw.get("repo") or "").strip()
        if not repo:
            raise ValueError(f"queue item {lane!r} has no 'repo'")

        def _str_tuple(key: str) -> tuple[str, ...]:
            value = raw.get(key) or []
            if isinstance(value, str):
                return (value,)
            if isinstance(value, (list, tuple)):
                return tuple(str(v).strip() for v in value if str(v).strip())
            return ()

        depends = raw.get("depends_on") or []
        if isinstance(depends, str):
            depends = (depends,)
        return cls(
            lane=lane,
            repo=repo,
            task=str(raw.get("task") or ""),
            ref=str(raw.get("ref") or lane),
            area=str(raw.get("area") or ""),
            size=str(raw.get("size") or ""),
            cluster=str(raw.get("cluster") or ""),
            evidence=str(raw.get("evidence") or ""),
            files=_str_tuple("files"),
            dbt_models=_str_tuple("dbt_models"),
            depends_on=tuple(str(d).strip() for d in depends if str(d).strip()),
        )


def load_queue(path: Path | str) -> tuple[DispatchItem, ...]:
    """Read a JSONL queue, skipping blank lines. Raises on a malformed item."""
    text = Path(path).expanduser().read_text(encoding="utf-8")
    items: list[DispatchItem] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno}: not valid JSON: {exc}") from exc
        try:
            items.append(DispatchItem.from_dict(raw))
        except ValueError as exc:
            raise ValueError(f"{path}:{lineno}: {exc}") from exc
    return tuple(items)


def render_task_file(item: DispatchItem, *, fences: str = "") -> str:
    """The task file handed to ``lane run --task-file``.

    Triage's findings (evidence, touched files, dbt models) are included so the
    implementer does not re-derive them, and the house fences are appended, never
    substituted for.
    """
    header = f"# Lane {item.lane} ({item.ref}, {item.repo}, {item.area}, size {item.size})"
    parts = [header, "", item.task, ""]
    if item.evidence:
        parts += [f"Evidence from triage: {item.evidence}", ""]
    if item.files:
        parts += [f"Files: {', '.join(item.files)}", ""]
    if item.dbt_models:
        parts += [f"dbt models: {', '.join(item.dbt_models)}", ""]
    if item.depends_on:
        parts += [f"Depends on: {', '.join(item.depends_on)}", ""]
    parts += [
        "RULES: targeted tests only, run them memory-capped; commit "
        "(never --no-verify), push, open ONE PR referencing "
        f"{item.ref}. Never kill processes by name/pattern."
    ]
    if fences:
        parts += ["", "===== STANDING FENCES =====", fences]
    return "\n".join(parts)


# ---------------------------------------------------------------- lane records


def _slugify(value: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "-" for c in value)


@dataclass(frozen=True)
class DispatchLane:
    """One lane's dispatch-level record.

    The two pid triples are recorded *before* the next tick runs, exactly as
    ``runner.py`` records a lane's identity before spawning its engine. That is
    what makes re-attaching on restart possible.
    """

    lane: str
    item: DispatchItem | None
    state: str = DISPATCH_QUEUED
    #: Identity of the ``fleet lane run`` child.
    lane_pid: int | None = None
    lane_pgid: int | None = None
    lane_starttime: int | None = None
    #: Identity of the gate child.
    gate_pid: int | None = None
    gate_pgid: int | None = None
    gate_starttime: int | None = None
    pr: int | None = None
    repo_path: str | None = None
    status_file: str | None = None
    worktree: str | None = None
    #: Terminal classification, e.g. ``approved``/``escalated``/``no_pr``.
    reason: str | None = None
    gate_exit: int | None = None
    error: str | None = None
    started_ts: float = 0.0
    updated_ts: float = 0.0

    @property
    def dependency_refs(self) -> tuple[str, ...]:
        """The queue item's ``depends_on``; empty for a lane with no item."""
        return self.item.depends_on if self.item is not None else ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "item": self.item.to_dict() if self.item else None,
            "state": self.state,
            "lane_pid": self.lane_pid,
            "lane_pgid": self.lane_pgid,
            "lane_starttime": self.lane_starttime,
            "gate_pid": self.gate_pid,
            "gate_pgid": self.gate_pgid,
            "gate_starttime": self.gate_starttime,
            "pr": self.pr,
            "repo_path": self.repo_path,
            "status_file": self.status_file,
            "worktree": self.worktree,
            "reason": self.reason,
            "gate_exit": self.gate_exit,
            "error": self.error,
            "started_ts": self.started_ts,
            "updated_ts": self.updated_ts,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DispatchLane:
        def _opt_int(key: str) -> int | None:
            value = raw.get(key)
            return int(value) if isinstance(value, (int, float)) else None

        item_raw = raw.get("item")
        item = DispatchItem.from_dict(item_raw) if isinstance(item_raw, dict) else None
        return cls(
            lane=str(raw.get("lane") or ""),
            item=item,
            state=str(raw.get("state") or DISPATCH_QUEUED),
            lane_pid=_opt_int("lane_pid"),
            lane_pgid=_opt_int("lane_pgid"),
            lane_starttime=_opt_int("lane_starttime"),
            gate_pid=_opt_int("gate_pid"),
            gate_pgid=_opt_int("gate_pgid"),
            gate_starttime=_opt_int("gate_starttime"),
            pr=_opt_int("pr"),
            repo_path=raw.get("repo_path"),
            status_file=raw.get("status_file"),
            worktree=raw.get("worktree"),
            reason=raw.get("reason"),
            gate_exit=_opt_int("gate_exit"),
            error=raw.get("error"),
            started_ts=float(raw.get("started_ts") or 0.0),
            updated_ts=float(raw.get("updated_ts") or 0.0),
        )


@dataclass(frozen=True)
class DispatchState:
    """Everything the dispatcher needs to survive a restart.

    Immutable: :func:`plan_tick` takes one and returns actions, and the loop
    folds those actions in with :func:`dataclasses.replace`. That is what lets
    the whole scheduler be tested as a pure function.
    """

    operator: str
    queue_path: str = ""
    lanes: dict[str, DispatchLane] = field(default_factory=dict)
    started_ts: float = 0.0
    #: Live children, by lane. Never persisted — a restart re-derives liveness
    #: from the recorded pid triples instead (see :func:`reattach`).
    procs: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def lane(self, name: str) -> DispatchLane | None:
        return self.lanes.get(name)

    def lanes_in(self, states: frozenset[str]) -> list[DispatchLane]:
        return [lane for lane in self.lanes.values() if lane.state in states]

    def to_dict(self) -> dict[str, Any]:
        return {
            "operator": self.operator,
            "queue_path": self.queue_path,
            "started_ts": self.started_ts,
            "lanes": {name: lane.to_dict() for name, lane in sorted(self.lanes.items())},
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DispatchState:
        lanes_raw = raw.get("lanes")
        lanes: dict[str, DispatchLane] = {}
        if isinstance(lanes_raw, dict):
            for name, entry in lanes_raw.items():
                if isinstance(entry, dict):
                    lanes[str(name)] = DispatchLane.from_dict(entry)
        return cls(
            operator=str(raw.get("operator") or ""),
            queue_path=str(raw.get("queue_path") or ""),
            lanes=lanes,
            started_ts=float(raw.get("started_ts") or 0.0),
        )


# ---------------------------------------------------------------- persistence


def dispatch_dir() -> Path:
    """``~/.agent-fleet/lanes/dispatch`` (honours ``AGENT_FLEET_HOME``)."""
    return lanes_dir() / "dispatch"


def dispatch_state_path(operator: str) -> Path:
    """One dispatcher's durable state, namespaced by operator.

    The operator is the first path component for the same reason it is in
    ``registry.lane_state_path``: two operators dispatch the same queues against
    the same repos, and neither may clobber the other's record.
    """
    return dispatch_dir() / operator / "state.json"


def load_state(operator: str, *, queue_path: str = "") -> DispatchState:
    """Load durable state for *operator*, or a fresh one.

    Never raises on a corrupt or missing file: a dispatcher that cannot read its
    state restarts the queue rather than refusing to run.
    """
    path = dispatch_state_path(operator)
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            logger.warning("dispatch state at %s is unreadable; starting fresh", path)
        else:
            if isinstance(raw, dict):
                state = DispatchState.from_dict(raw)
                if queue_path and not state.queue_path:
                    state = replace(state, queue_path=queue_path)
                return state
    return DispatchState(
        operator=operator,
        queue_path=queue_path,
        started_ts=time.time(),
    )


def save_state(state: DispatchState) -> Path:
    """Persist *state* atomically (tmp + replace), as ``registry.save_record``."""
    path = dispatch_state_path(state.operator)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state.to_dict(), indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    return path


def merge_queue(state: DispatchState, items: Sequence[DispatchItem]) -> DispatchState:
    """Fold *items* into *state*, keyed by lane name.

    A lane already known keeps its recorded state; a new one is queued. This is
    the dict lookup that the shell driver got wrong with ``next(...)`` — a lane
    with no item is never looked up, only recorded.
    """
    lanes = dict(state.lanes)
    now = time.time()
    for item in items:
        existing = lanes.get(item.lane)
        if existing is None:
            lanes[item.lane] = DispatchLane(
                lane=item.lane,
                item=item,
                state=DISPATCH_QUEUED,
                started_ts=now,
                updated_ts=now,
            )
        elif existing.item is None:
            # A lane re-registered from a queue gains its item, keeps its state.
            lanes[item.lane] = replace(existing, item=item, updated_ts=now)
    return replace(state, lanes=lanes)


# ---------------------------------------------------------------- liveness


def lane_process_alive(lane: DispatchLane) -> bool:
    """Whether this lane's *own* recorded process is still running.

    Both halves matter. ``process_alive`` alone would treat a recycled pid as the
    original lane; the ``starttime`` fingerprint is what distinguishes them. A
    lane whose recorded identity is gone is relaunchable, which is exactly what
    makes a crashed lane recover instead of hanging forever.
    """
    return process_identity_alive(lane.lane_pid, lane.lane_starttime)


def gate_process_alive(lane: DispatchLane) -> bool:
    return process_identity_alive(lane.gate_pid, lane.gate_starttime)


def process_identity_alive(pid: int | None, starttime: int | None) -> bool:
    """True only when *pid* is alive **and** is the process we recorded.

    Never matches on a command line. The shell driver asked ``ps -eo args``
    whether ``--lane X`` appeared, which is how two operators adopted each
    other's lanes.
    """
    if not pid:
        return False
    if not registry.process_alive(pid):
        return False
    if starttime is None:
        return True
    current = registry.process_starttime(pid)
    return current is None or current == starttime


# ---------------------------------------------------------------- ordering


def cluster_rank(cluster: str, order: Sequence[str]) -> int:
    """Sort key for *cluster*: configured order first, unknown clusters last."""
    if cluster in order:
        return list(order).index(cluster)
    return len(order)


def order_lanes(state: DispatchState, *, cluster_order: Sequence[str] = ()) -> list[DispatchLane]:
    """Queued lanes in launch order: cluster order, then queue order.

    Stable, so lanes sharing a cluster launch in the order the queue listed them.
    Unknown clusters sort after every configured one, alphabetically, rather than
    jumping the queue.
    """
    queued = [lane for lane in state.lanes.values() if lane.state == DISPATCH_QUEUED]
    return sorted(
        queued,
        key=lambda lane: (
            cluster_rank(lane.item.cluster if lane.item else "", cluster_order),
            (lane.item.cluster if lane.item else ""),
            lane.lane,
        ),
    )


# ---------------------------------------------------------------- the plan


@dataclass(frozen=True)
class LaunchLane:
    """Start ``fleet lane run --no-gate`` for *lane*."""

    lane: str


@dataclass(frozen=True)
class LaunchGate:
    """Start the gate for *lane*, which has a verified PR at *pr*."""

    lane: str
    pr: int


@dataclass(frozen=True)
class ReapLane:
    """The lane child exited; read its result and record the PR (or the failure)."""

    lane: str
    exit_code: int | None


@dataclass(frozen=True)
class ReapGate:
    """The gate child exited; classify it from the lane's status line."""

    lane: str
    exit_code: int | None


@dataclass(frozen=True)
class FinishLane:
    """The lane is over. *reason* is the terminal classification."""

    lane: str
    reason: str
    exit_code: int | None = None


@dataclass(frozen=True)
class RecoverLane:
    """The recorded process vanished without exiting; relaunch the lane."""

    lane: str
    reason: str


Action = LaunchLane | LaunchGate | ReapLane | ReapGate | FinishLane | RecoverLane


def terminal_refs(state: DispatchState) -> set[str]:
    """Refs of every terminal lane — the released dependency set.

    A dependency is released when its lane is *terminal*, not when it is
    approved. ``depends_on`` exists to serialise lanes that touch the same
    files; a lane that escalated must not deadlock the rest of its chain, and
    holding the chain hostage to a failure is strictly worse than letting it
    proceed.
    """
    refs: set[str] = set()
    for lane in state.lanes.values():
        if lane.state not in DISPATCH_TERMINAL:
            continue
        if lane.item is not None:
            refs.add(lane.item.ref or lane.item.lane)
        refs.add(lane.lane)
    return refs


def known_dependency_keys(state: DispatchState) -> set[str]:
    """Every ref and lane name this dispatch can still resolve.

    ``depends_on`` is written in the queue in either spelling — ``R-1234`` (a
    triage ref) or a bare lane name — and both must be matched against what the
    queue can actually produce. Matching only lane names would make every
    ref-based dependency look like it names something absent from the queue, and
    therefore unresolvable, so a cycle would launch instead of being reported.
    """
    keys: set[str] = set()
    for lane in state.lanes.values():
        if lane.item is None:
            continue
        keys.add(lane.lane)
        keys.add(lane.item.ref or lane.lane)
    return keys


def dependency_satisfied(lane: DispatchLane, released: set[str], known: set[str]) -> bool:
    """Whether every dependency of *lane* is terminal.

    A dependency naming something absent from the queue is ignored: it can never
    become terminal, and blocking on it forever is the deadlock the shell driver
    walked into.
    """
    deps = lane.dependency_refs
    if not deps:
        return True
    for dep in deps:
        if dep in released:
            continue
        if dep in known:
            return False
    return True


def blocked_lanes(state: DispatchState, *, cluster_order: Sequence[str] = ()) -> list[str]:
    """Queued lanes that can never launch because a dependency is unresolvable.

    A cycle (``a`` waits on ``b`` waits on ``a``) and a dependency on a lane that
    is itself stuck behind the cycle are both permanently blocked: no run order
    satisfies them. Without this the dispatcher would sit on a full queue of
    impossible lanes forever, which is the silent-hang version of the shell
    driver's ``StopIteration``.
    """
    queued = {lane.lane: lane for lane in state.lanes.values() if lane.state == DISPATCH_QUEUED}
    if not queued:
        return []
    released = terminal_refs(state)
    known = known_dependency_keys(state)
    order = order_lanes(state, cluster_order=cluster_order)
    settled: set[str] = set()
    progressed = True
    while progressed:
        progressed = False
        for lane in order:
            if lane.lane in settled or lane.lane not in queued:
                continue
            if dependency_satisfied(lane, released | settled, known):
                settled.add(lane.lane)
                progressed = True
    return [lane.lane for lane in order if lane.lane not in settled]


def plan_tick(
    state: DispatchState,
    *,
    max_lanes: int = DEFAULT_MAX_LANES,
    max_gates: int = DEFAULT_MAX_GATES,
    psi: pressure.Throttle | None = None,
    psi_avg10_max: float = pressure.DEFAULT_PSI_AVG10_MAX,
    cluster_order: Sequence[str] = (),
    exited: Mapping[str, int] | None = None,
) -> list[Action]:
    """Decide everything to do this tick. Pure: no IO, no clock, no subprocess.

    The counts below include actions *this tick* has already decided, which is
    what makes ``--max-gates`` exact rather than off by one. The shell driver
    released its gate cap one tick late and eighteen gates went out together.

    *exited* maps lane name to an observed exit code for a child this process
    actually spawned. It is a **parameter, not a call**, for a concrete reason:
    ``os.kill(pid, 0)`` succeeds on a zombie, so a lane that has exited but has
    not been reaped still looks alive to a pid probe. Only ``Popen.poll`` both
    answers correctly and clears the zombie, and the dispatcher has the handle
    for exactly the children it started. Lanes carried in from a *restart* have
    no handle, so those fall back to the pid+starttime fingerprint.
    """
    actions: list[Action] = []
    observed = exited or {}

    # --- 1. reattach to whatever is still running -------------------------
    for lane in state.lanes.values():
        code = observed.get(lane.lane)
        if lane.state == DISPATCH_RUNNING:
            if code is None and lane_process_alive(lane):
                continue
            if code is None and lane.lane_pid is None:
                # No identity was ever recorded (state written mid-spawn, or a
                # crash between the decision and the record). Reclaim the slot
                # rather than leaking it forever.
                actions.append(RecoverLane(lane.lane, reason="no_lane_process"))
            else:
                actions.append(ReapLane(lane.lane, exit_code=code))
        elif lane.state == DISPATCH_GATING:
            # A gate is done when its handle reported an exit OR when its
            # recorded identity is gone. Requiring the handle alone would hang
            # a gate we did not spawn (a restart), and requiring the probe alone
            # would miss a gate that has exited but not been reaped.
            if code is not None or not gate_process_alive(lane):
                actions.append(ReapGate(lane.lane, exit_code=code))

    # --- 2. lanes whose process finished ----------------------------------
    finished = [action.lane for action in actions if isinstance(action, ReapLane)]
    gating = [action.lane for action in actions if isinstance(action, ReapGate)]

    # --- 3. promote pr -> gating, then fill the remaining gate slots --------
    # Counting what this tick already decided keeps the cap exact.
    gates_in_flight = len(state.lanes_in(GATE_SLOT_STATES)) + len(gating)
    released = terminal_refs(state)

    # Gate capacity is as scarce as lane capacity, so it follows the same
    # cluster order: a C9 lane must not consume a gate slot ahead of a C0 lane
    # that is already waiting for one.
    pending_gates = sorted(
        (lane for lane in state.lanes.values() if lane.state == DISPATCH_PR),
        key=lambda lane: (
            cluster_rank(lane.item.cluster if lane.item else "", cluster_order),
            (lane.item.cluster if lane.item else ""),
            lane.lane,
        ),
    )
    for lane in pending_gates:
        if gates_in_flight >= max_gates:
            break
        if lane.pr is None:
            # A lane with no PR can never be gated. Finishing it here (rather
            # than launching a gate for it) is the crash the shell driver took
            # when a finished lane was not in its queue.
            actions.append(FinishLane(lane.lane, reason=UNKNOWN_ITEM))
            continue
        actions.append(LaunchGate(lane.lane, pr=lane.pr))
        gates_in_flight += 1

    # --- 4. launch new lanes ----------------------------------------------
    lanes_in_flight = len(state.lanes_in(LANE_SLOT_STATES)) + len(finished)
    throttled = pressure.throttled(psi, avg10_max=psi_avg10_max) if psi is not None else False
    known = known_dependency_keys(state)

    if throttled:
        logger.debug(
            "dispatch %s: cpu pressure %.1f%% >= %.0f%%; not launching lanes",
            state.operator,
            psi.some_avg10 if psi and psi.some_avg10 is not None else 0.0,
            psi_avg10_max,
        )

    for lane in order_lanes(state, cluster_order=cluster_order):
        if throttled:
            break
        if lanes_in_flight >= max_lanes:
            break
        if lane.item is None:
            # A lane with no queue item can never be launched: there is no task
            # to give the implementer. Recorded, not looked up, not raised.
            actions.append(FinishLane(lane.lane, reason=UNKNOWN_ITEM))
            continue
        if not dependency_satisfied(lane, released, known):
            continue
        actions.append(LaunchLane(lane.lane))
        lanes_in_flight += 1

    return actions


# ---------------------------------------------------------------- gate commands


def gate_argv(
    template: str | None,
    *,
    lane: str,
    pr: int,
    repo: str,
    operator: str = "",
    slug: str = "",
    judge_engine: str | None = None,
    worktree: str | None = None,
) -> list[str]:
    """Build the gate argv for *lane*.

    A template is expanded with ``{lane}``/``{pr}``/``{repo}``/``{operator}``/
    ``{slug}`` and split with :func:`shlex.split`, then executed **without a
    shell**. A template containing ``;`` or ``&&`` therefore stays a single
    argv entry instead of becoming a second command.

    With no template the built-in ``agent-fleet gate`` is used, which is the same
    seam ``lane run`` uses — so a repo that needs no external gate script needs
    no configuration at all.
    """
    if not template:
        argv = [
            "agent-fleet",
            "gate",
            "--lane",
            lane,
            "--repo",
            slug or repo,
            "--pr",
            str(pr),
            "--head-ref",
            f"fb/{lane}",
        ]
        if judge_engine:
            argv += ["--judge-engine", judge_engine]
        return argv

    expanded = expand_template(template, lane=lane, operator=operator)
    for key, value in (("pr", str(pr)), ("repo", repo), ("slug", slug or repo)):
        expanded = expanded.replace("{" + key + "}", value)
    try:
        argv = shlex.split(expanded)
    except ValueError as exc:
        raise ValueError(f"gate command template is not parseable: {exc}") from exc
    if not argv:
        raise ValueError("gate command template expanded to an empty command")
    if worktree:
        argv += ["--worktree", worktree]
    return argv


# ---------------------------------------------------------------- the loop


@dataclass
class DispatchSummary:
    """What one ``fleet dispatch`` run achieved."""

    operator: str
    lanes: int = 0
    launched: int = 0
    gated: int = 0
    approved: int = 0
    escalated: int = 0
    no_pr: int = 0
    errors: int = 0
    state: DispatchState | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "operator": self.operator,
            "lanes": self.lanes,
            "launched": self.launched,
            "gated": self.gated,
            "approved": self.approved,
            "escalated": self.escalated,
            "no_pr": self.no_pr,
            "errors": self.errors,
        }

    def exit_code(self) -> int:
        """0 when every lane finished cleanly, 1 when any escalated or errored."""
        return 0 if not (self.escalated or self.errors) else 1


def _event(state: DispatchState, lane: str, event: str, **fields: Any) -> None:  # noqa: ANN401
    append_event(state.operator, lane, event, **fields)


def _status_for(lane: DispatchLane) -> str:
    """The lane's status file contents.

    Read from the file the lane's own gate wrote, never by scanning a shared
    log — that is how two operators read each other's verdicts. The *whole*
    file is returned, not just its last line, so :func:`classify_status` can see
    a trailing ``NEEDS-ESCALATION`` retracting an earlier approval.
    """
    if not lane.status_file:
        return ""
    try:
        return Path(lane.status_file).expanduser().read_text(encoding="utf-8")
    except OSError:
        return ""


def classify_status(status_line: str, *, exit_code: int | None = None) -> str:
    """Terminal reason from a gate's status line.

    The **last** non-empty line is the verdict, matching the gate's own
    contract: reading the whole transcript is how
    ``NEEDS-ESCALATION: did not APPROVE the fix`` gets mistaken for an approval.
    An ``APPROVED`` token *anywhere* in the text is ignored in favour of that
    last line, but a trailing ``NEEDS-ESCALATION`` — which is what a gate that
    approved and then failed to complete its rounds writes — is an escalation.
    A non-zero exit with no approval line is an escalation too.
    """
    lines = [line.strip() for line in (status_line or "").splitlines() if line.strip()]
    last = lines[-1] if lines else ""
    if ESCALATION_TOKEN in last:
        return "escalated"
    if APPROVED_TOKEN in last:
        return "approved"
    if exit_code not in (0, None):
        return f"escalated (gate exit {exit_code})"
    return "escalated (no approval line)"


def read_lane_result(
    stdout: str,
) -> tuple[int | None, str | None, str | None]:
    """Extract ``(pr, worktree, detail)`` from a ``lane run --json`` result.

    The PR comes from the lane's own JSON, not from a ``ps`` scan or a fresh
    ``gh`` call: the lane already resolved and verified the binding, so re-asking
    risks reading a different PR than the one the lane guaranteed.
    """
    payload = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and "state" in candidate:
            payload = candidate
    if not isinstance(payload, dict):
        return None, None, None
    pr_raw = payload.get("pr")
    pr = int(pr_raw) if isinstance(pr_raw, (int, float)) else None
    worktree = payload.get("worktree")
    detail = payload.get("detail") or payload.get("reason") or ""
    return pr, (str(worktree) if worktree else None), (str(detail) or None)


def run_dispatch(
    *,
    operator: str,
    queue_path: Path | str,
    items: Sequence[DispatchItem] | None = None,
    repos: dict[str, str] | None = None,
    max_lanes: int = DEFAULT_MAX_LANES,
    max_gates: int = DEFAULT_MAX_GATES,
    gate_cmd: str | None = None,
    cluster_order: Sequence[str] = (),
    owner: str = DEFAULT_OWNER,
    fences: str = "",
    judge_engine: str | None = None,
    state: DispatchState | None = None,
    tick_seconds: float = DEFAULT_TICK_SECONDS,
    run_dir: Path | str | None = None,
    spawn: Callable[..., SpawnedProc] | None = None,
    psi_reader: Callable[[], pressure.Throttle] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    psi_avg10_max: float = pressure.DEFAULT_PSI_AVG10_MAX,
) -> DispatchSummary:
    """Dispatch *queue_path* to completion.

    The loop is a thin interpreter over :func:`plan_tick`. It owns the three
    things the pure function must not: spawning children, persisting state, and
    converting a child's exit into the next tick's input.

    Every action is executed inside its own ``try``/``except``. A single lane
    that fails to spawn, a worktree that will not materialise, a template that
    will not parse — each is recorded on that lane and the loop continues. The
    shell driver had no such guard, which is why one unparseable queue item took
    every remaining lane down with it.
    """
    queue = Path(queue_path).expanduser()
    if items is None:
        items = load_queue(queue)
    if not repos:
        raise ValueError("dispatch needs a repo map (repo name -> repo path)")
    missing = sorted({item.repo for item in items} - set(repos))
    if missing:
        raise ValueError(f"dispatch: no repo path configured for: {', '.join(missing)}")

    current = state or load_state(operator, queue_path=str(queue))
    current = merge_queue(current, items)
    save_state(current)

    summary = DispatchSummary(operator=operator, lanes=len(current.lanes))
    start = _real_spawn() if spawn is None else spawn
    read_psi = psi_reader or (lambda: pressure.read_throttle())
    out_root = Path(run_dir).expanduser() if run_dir else dispatch_dir() / operator

    while True:
        psi = read_psi()
        actions = plan_tick(
            current,
            max_lanes=max_lanes,
            max_gates=max_gates,
            psi=psi,
            psi_avg10_max=psi_avg10_max,
            cluster_order=cluster_order,
            exited=_poll_exited(current),
        )
        if not actions:
            # Nothing launched, nothing reaped, and no slot freed: the only way
            # out is to finish the lanes that can never run. A dependency cycle
            # would otherwise leave the dispatcher waiting on a queue that can
            # never drain.
            stuck = blocked_lanes(current, cluster_order=cluster_order)
            if stuck and _complete(current):
                for name in stuck:
                    current = _finish(current, name, "dependency_deadlock", summary=summary)
                save_state(current)
                break
            if _complete(current):
                break
            sleep(tick_seconds)
            continue

        for action in actions:
            try:
                current = _apply(
                    current,
                    action,
                    summary=summary,
                    repos=repos,
                    owner=owner,
                    fences=fences,
                    gate_cmd=gate_cmd,
                    judge_engine=judge_engine,
                    spawn=start,
                    out_root=out_root,
                )
            except Exception as exc:
                name = getattr(action, "lane", "?")
                current = _record_error(current, name, exc)
                summary.errors += 1
        save_state(current)

    summary.state = current
    _event(
        current,
        "_dispatch",
        "dispatch.finished",
        lanes=summary.lanes,
        approved=summary.approved,
        escalated=summary.escalated,
        no_pr=summary.no_pr,
        errors=summary.errors,
    )
    save_state(current)
    return summary


def _poll_exited(state: DispatchState) -> dict[str, int]:
    """Observed exit codes for the children *this* process spawned.

    ``poll()`` is the only reliable answer for a child: ``os.kill(pid, 0)``
    succeeds on a zombie, so an exited-but-unreaped lane would look alive to a
    pid probe and hold its slot until the dispatcher itself died. Calling poll
    also reaps the zombie, which is why the result is threaded into ``plan_tick``
    rather than re-probed there.
    """
    exited: dict[str, int] = {}
    for name, proc in state.procs.items():
        if proc is None:
            continue
        try:
            code = proc.poll()
        except OSError:  # pragma: no cover - defensive
            continue
        if code is not None:
            exited[name] = int(code)
    return exited


def _complete(state: DispatchState) -> bool:
    """True when no lane can make further progress."""
    return not any(lane.state in LANE_SLOT_STATES for lane in state.lanes.values())


def _record_error(state: DispatchState, lane_name: str, exc: Exception) -> DispatchState:
    """Mark a lane terminal-failed, so it is reported rather than retried forever.

    The state written here must be the one :func:`plan_tick` reads. Writing
    ``done`` while the record still says ``queued`` is what made one failed
    launch turn into an infinite relaunch loop: the error handler "finished" the
    lane, and the very next tick saw a queued lane and started it again.
    """
    lanes = dict(state.lanes)
    record = lanes.get(lane_name)
    detail = f"{type(exc).__name__}: {exc}"[:500]
    lanes[lane_name] = replace(
        record if record is not None else DispatchLane(lane=lane_name, item=None),
        state=DISPATCH_FAILED,
        reason="error",
        error=detail,
        updated_ts=time.time(),
    )
    new_state = replace(state, lanes=lanes)
    _event(new_state, lane_name, "dispatch.error", detail=detail)
    return new_state


def _finish(
    state: DispatchState,
    lane_name: str,
    reason: str,
    *,
    exit_code: int | None = None,
    summary: DispatchSummary | None = None,
) -> DispatchState:
    """Mark a lane terminal and, when given, tally it.

    The tally lives here rather than at the call sites because a lane can reach
    its terminal state from three directions — a ``FinishLane`` action, a lane
    that produced no PR, and a gate that returned a verdict — and counting only
    one of them is how a run reports "0 escalated" for a queue full of
    escalations.
    """
    lanes = dict(state.lanes)
    record = lanes.get(lane_name)
    if record is None:
        record = DispatchLane(lane=lane_name, item=None)
    lanes[lane_name] = replace(
        record, state=DISPATCH_DONE, reason=reason, gate_exit=exit_code, updated_ts=time.time()
    )
    if summary is not None:
        _tally(summary, reason)
    new_state = replace(state, lanes=lanes)
    _event(new_state, lane_name, "dispatch.done", reason=reason, exit_code=exit_code)
    return new_state


def _launch_lane(
    state: DispatchState,
    action: LaunchLane,
    *,
    repos: dict[str, str],
    owner: str,
    fences: str,
    out_root: Path,
    spawn: Callable[..., SpawnedProc],
    summary: DispatchSummary | None = None,
) -> DispatchState:
    record = state.lanes.get(action.lane)
    if record is None or record.item is None:
        return _finish(state, action.lane, UNKNOWN_ITEM, summary=summary)

    item = record.item
    repo_path = Path(repos[item.repo]).expanduser()
    if not repo_path.is_dir():
        raise FileNotFoundError(f"repo path does not exist: {repo_path}")

    out_root.mkdir(parents=True, exist_ok=True)
    task_file = out_root / "prompts" / f"{item.lane}.task.md"
    task_file.parent.mkdir(parents=True, exist_ok=True)
    task_file.write_text(render_task_file(item, fences=fences), encoding="utf-8")

    log = out_root / "runs" / f"lane-{item.lane}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    status_file = out_root / "lanes" / f"{item.lane}.status"
    status_file.parent.mkdir(parents=True, exist_ok=True)

    argv = [
        "fleet",
        "lane",
        "run",
        "--operator",
        state.operator,
        "--lane",
        item.lane,
        "--repo-path",
        str(repo_path),
        "--task-file",
        str(task_file),
        "--expected-repo",
        f"{owner}/{item.repo}",
        "--status-file",
        str(status_file),
        "--no-gate",
        "--json",
    ]
    # start_new_session: the child leads its own process group, which is what
    # `lanes stop` needs to reach exactly this lane and nothing else.
    proc = spawn(argv, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

    lanes = dict(state.lanes)
    # The live handle MUST be recorded here. `os.kill(pid, 0)` answers for a
    # zombie, so a child that has exited but not been reaped still looks alive;
    # only the handle can report its exit and clear it. Without this the lane
    # would hold its slot until the dispatcher itself died.
    procs = dict(state.procs)
    procs[item.lane] = proc
    lanes[item.lane] = replace(
        record,
        state=DISPATCH_RUNNING,
        lane_pid=proc.pid,
        lane_pgid=_pgid(proc.pid),
        lane_starttime=process_starttime(proc.pid),
        repo_path=str(repo_path),
        status_file=str(status_file),
        updated_ts=time.time(),
    )
    new_state = replace(state, lanes=lanes, procs=procs)
    _event(new_state, item.lane, "dispatch.launched", repo=item.repo, pid=proc.pid)
    return new_state


def _reap_lane(
    state: DispatchState,
    action: ReapLane,
    *,
    out_root: Path,
    summary: DispatchSummary | None = None,
) -> DispatchState:
    record = state.lanes.get(action.lane)
    if record is None:
        return state
    exit_code = action.exit_code
    if exit_code is None:
        proc = state.procs.get(action.lane)
        exit_code = proc.poll() if proc is not None else None

    log = out_root / "runs" / f"lane-{action.lane}.log"
    try:
        stdout = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        stdout = ""
    pr, worktree, detail = read_lane_result(stdout)

    lanes = dict(state.lanes)
    procs = dict(state.procs)
    procs.pop(action.lane, None)
    base = replace(
        record,
        lane_pid=None,
        lane_pgid=None,
        lane_starttime=None,
        worktree=worktree or record.worktree,
        updated_ts=time.time(),
    )
    if pr is not None:
        lanes[action.lane] = replace(base, state=DISPATCH_PR, pr=pr)
        new_state = replace(state, lanes=lanes, procs=procs)
        _event(new_state, action.lane, "dispatch.pr", pr=pr, exit_code=exit_code)
        return new_state

    procs_state = replace(state, lanes=lanes, procs=procs)
    if summary is not None:
        _tally(summary, "no_pr")
    finished = _finish(procs_state, action.lane, "no_pr", exit_code=exit_code)
    # _finish already appended the terminal event; the lane's own detail (why
    # there was no PR) belongs on the record, not as a second event.
    lanes2 = dict(finished.lanes)
    lanes2[action.lane] = replace(lanes2[action.lane], error=detail)
    return replace(finished, lanes=lanes2)


def _recover_lane(state: DispatchState, action: RecoverLane) -> DispatchState:
    record = state.lanes.get(action.lane)
    if record is None:
        return state
    lanes = dict(state.lanes)
    lanes[action.lane] = replace(
        record,
        state=DISPATCH_QUEUED,
        lane_pid=None,
        lane_pgid=None,
        lane_starttime=None,
        updated_ts=time.time(),
    )
    new_state = replace(state, lanes=lanes)
    _event(new_state, action.lane, "dispatch.recovered", reason=action.reason)
    return new_state


def _launch_gate(
    state: DispatchState,
    action: LaunchGate,
    *,
    gate_cmd: str | None,
    judge_engine: str | None,
    out_root: Path,
    spawn: Callable[..., SpawnedProc],
    summary: DispatchSummary | None = None,
) -> DispatchState:
    record = state.lanes.get(action.lane)
    if record is None or record.item is None or record.pr is None:
        return _finish(state, action.lane, UNKNOWN_ITEM, summary=summary)

    item = record.item
    argv = gate_argv(
        gate_cmd,
        lane=item.lane,
        pr=record.pr,
        repo=item.repo,
        operator=state.operator,
        slug=f"{DEFAULT_OWNER}/{item.repo}",
        judge_engine=judge_engine,
        worktree=record.worktree,
    )
    log = out_root / "gate" / f"gate-{item.lane}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.open("w", encoding="utf-8").close()

    proc = spawn(argv, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

    lanes = dict(state.lanes)
    procs = dict(state.procs)
    procs[item.lane] = proc
    lanes[item.lane] = replace(
        record,
        state=DISPATCH_GATING,
        gate_pid=proc.pid,
        gate_pgid=_pgid(proc.pid),
        gate_starttime=process_starttime(proc.pid),
        updated_ts=time.time(),
    )
    new_state = replace(state, lanes=lanes, procs=procs)
    _event(new_state, item.lane, "dispatch.gate.started", pr=record.pr, pid=proc.pid)
    return new_state


def _reap_gate(
    state: DispatchState, action: ReapGate, *, summary: DispatchSummary | None = None
) -> DispatchState:
    record = state.lanes.get(action.lane)
    if record is None:
        return state
    exit_code = action.exit_code
    if exit_code is None:
        proc = state.procs.get(action.lane)
        exit_code = proc.poll() if proc is not None else None

    status_line = _status_for(record)
    reason = classify_status(status_line, exit_code=exit_code)
    procs = dict(state.procs)
    procs.pop(action.lane, None)
    finished = _finish(state, action.lane, reason, exit_code=exit_code, summary=summary)
    return replace(finished, procs=procs)


def _pgid(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def _real_spawn() -> Callable[..., SpawnedProc]:
    def spawn(
        argv: Sequence[str],
        *,
        stdout: int | IO[str] | None,
        stderr: int | IO[str] | None,
        start_new_session: bool,
    ) -> SpawnedProc:
        return subprocess.Popen(
            list(argv),
            stdout=stdout,
            stderr=stderr,
            start_new_session=start_new_session,
        )

    return spawn


def _apply(
    state: DispatchState,
    action: Action,
    *,
    summary: DispatchSummary,
    repos: dict[str, str],
    owner: str,
    fences: str,
    gate_cmd: str | None,
    judge_engine: str | None,
    spawn: Callable[..., SpawnedProc],
    out_root: Path,
) -> DispatchState:
    """Execute one planned action, folding its result into the state."""
    if isinstance(action, LaunchLane):
        summary.launched += 1
        return _launch_lane(
            state,
            action,
            repos=repos,
            owner=owner,
            fences=fences,
            out_root=out_root,
            spawn=spawn,
            summary=summary,
        )
    if isinstance(action, LaunchGate):
        return _launch_gate(
            state,
            action,
            gate_cmd=gate_cmd,
            judge_engine=judge_engine,
            out_root=out_root,
            spawn=spawn,
            summary=summary,
        )
    if isinstance(action, ReapLane):
        return _reap_lane(state, action, out_root=out_root, summary=summary)
    if isinstance(action, ReapGate):
        return _reap_gate(state, action, summary=summary)
    if isinstance(action, RecoverLane):
        return _recover_lane(state, action)
    if isinstance(action, FinishLane):
        # _finish tallies; calling _tally here too would double-count.
        return _finish(
            state, action.lane, action.reason, exit_code=action.exit_code, summary=summary
        )
    return state


#: Terminal reasons that are a dispatcher problem rather than a lane outcome.
#: They all count as errors: the operator has to look at the queue.
ERROR_REASONS = (UNKNOWN_ITEM, "error", "dependency_deadlock")


def _tally(summary: DispatchSummary, reason: str) -> None:
    if reason == "approved":
        summary.approved += 1
    elif reason == "no_pr":
        summary.no_pr += 1
    elif reason.startswith("escalated"):
        summary.escalated += 1
    elif reason in ERROR_REASONS:
        summary.errors += 1


def render_summary(summary: DispatchSummary) -> str:
    """A short operator-facing report of one dispatch run."""
    state = summary.state
    lines = [
        f"dispatch {summary.operator}: {summary.lanes} lane(s), "
        f"{summary.launched} launched, {summary.approved} approved, "
        f"{summary.escalated} escalated, {summary.no_pr} no-pr, {summary.errors} error(s)"
    ]
    if state is None:
        return "\n".join(lines)
    for lane in sorted(state.lanes.values(), key=lambda item: item.lane):
        detail = lane.reason or ""
        if lane.pr:
            detail = f"PR #{lane.pr} {detail}".strip()
        lines.append(f"  {local_hhmmss()} {lane.lane:<28} {lane.state:<8} {detail}".rstrip())
    return "\n".join(lines)


__all__ = [
    "DEFAULT_MAX_GATES",
    "DEFAULT_MAX_LANES",
    "DEFAULT_OWNER",
    "DEFAULT_TICK_SECONDS",
    "DISPATCH_DONE",
    "DISPATCH_GATING",
    "DISPATCH_PR",
    "DISPATCH_QUEUED",
    "DISPATCH_RUNNING",
    "UNKNOWN_ITEM",
    "Action",
    "DispatchItem",
    "DispatchLane",
    "DispatchState",
    "DispatchSummary",
    "FinishLane",
    "LaunchGate",
    "LaunchLane",
    "ReapGate",
    "ReapLane",
    "RecoverLane",
    "blocked_lanes",
    "classify_status",
    "cluster_rank",
    "dependency_satisfied",
    "dispatch_dir",
    "dispatch_state_path",
    "gate_argv",
    "gate_process_alive",
    "lane_process_alive",
    "load_queue",
    "load_state",
    "merge_queue",
    "order_lanes",
    "plan_tick",
    "process_identity_alive",
    "read_lane_result",
    "render_summary",
    "render_task_file",
    "run_dispatch",
    "save_state",
    "terminal_refs",
]
