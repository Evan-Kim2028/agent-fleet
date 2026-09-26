"""One supervisor for the whole pipeline, restart-safe by construction.

Serve is a single long-running process that owns a small set of component
processes — the dispatcher, the merge executor, the janitor — and is itself
supervised by an flock and a pid file. Every one of those three layers exists to
answer the same question: *after a restart, what is already running?*

Getting that wrong is how the bash fleet produced duplicate lanes. ``dispatch.py``
could be started twice, both copies would see free capacity, and both would
launch the same lane; the second ``fleet lane run`` would then fight the first
over the worktree. So:

**Re-attach, never double-start.** On boot the supervisor reads each
component's pid file and checks the fingerprint. A component that is still alive
is *adopted* — monitored, never respawned. A pid file whose process is gone, or
whose fingerprint no longer matches, is overwritten. The pid file is a cache of
the truth, never the truth itself.

**The supervisor lock is held for the supervisor's lifetime.** A second
``fleet serve`` for the same operator finds the flock taken and exits rather
than racing. This is checked before anything is spawned.

**Restart on exit, with backoff that remembers.** Backoff is exponential from
``backoff_initial_s`` to ``backoff_max_s`` and is computed from the monotonic
clock, so an NTP correction cannot shorten it. A component that exits cleanly
is restarted promptly; one that crashes backs off.

**Crash-loop detection counts crashes, not exits.** A component restarted three
times on purpose — by the no-progress rule, or because its command template
changed — is healthy. Counting those toward the crash budget would eventually
stop restarting a component that never crashed at all, so every exit carries a
``cause`` and only ``crash`` exits count. The crash history is persisted, so a
supervisor that restarts does not hand a crash-looping component a fresh
budget.

**One crash-looping component does not take the fleet down.** When the budget
is spent the component is marked ``crash_looping``, an error event is emitted,
and the other components keep running. That is the difference between a
supervisor and a monocle: the operator is told which one broke, and the rest of
the pipeline keeps shipping.
"""

from __future__ import annotations

import signal
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_fleet.serve.clock import SystemClock
from agent_fleet.serve.events import alert, emit_serve_event
from agent_fleet.serve.paths import (
    component_log_path,
    component_pid_path,
    ensure_serve_dir,
    read_json,
    write_json_atomic,
)
from agent_fleet.serve.procs import (
    ProcIdentity,
    escalate_kill_group,
    pid_alive,
    starttime_fingerprint,
    terminate_group,
)

if TYPE_CHECKING:
    from agent_fleet.serve.clock import Clock
    from agent_fleet.serve.config import ComponentSpec, ServeConfig

#: Why a child exited. Only ``crash`` counts toward the crash-loop budget.
CAUSE_CRASH = "crash"
CAUSE_EXIT = "exit"
CAUSE_REQUESTED = "requested"
CAUSE_CAPACITY = "capacity"

STATE_STOPPED = "stopped"
STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_BACKOFF = "backoff"
STATE_CRASH_LOOPING = "crash_looping"

#: How long a restart waits for the component it is replacing to be gone before
#: the replacement is spawned. Zero means "no grace": the old child is already
#: dead, or the operator has taken the only copy with a SIGKILL.
RESTART_KILL_GRACE_S = 0.0

#: Causes serve asked for, and which therefore do not consume crash budget. Any
#: other exit does: a component that stops itself, cleanly and repeatedly, is
#: the silently-broken shape the detector exists to find.
REQUESTED_CAUSES = frozenset({CAUSE_REQUESTED, CAUSE_CAPACITY})

#: Cap on remembered crash epochs per component, so a long-lived supervisor's
#: state file does not grow without bound.
_MAX_CRASH_HISTORY = 50


def expand_command(
    template: str,
    *,
    operator: str,
    serve_dir: Path,
    capacity_file: Path,
    targets: Any,  # noqa: ANN401
) -> list[str]:
    """Expand a command template into an argv list.

    ``{operator}``, ``{serve_dir}``, ``{capacity_file}``, ``{max_lanes}``,
    ``{max_gates}``, ``{gates_priority}``, ``{test_pool}``,
    ``{typecheck_pool}``. Substitution is plain string replacement, not
    ``str.format``, so a command containing a brace (a jq filter, a python
    one-liner) survives.

    ``shlex.split`` does the word splitting, so a template may be a whole shell
    command line. It is then exec'd **without a shell** — no shell means no
    ``$VAR`` expansion, no globbing and no second process to track, which is
    what makes ``pgid == pid`` hold and the group-termination check sound.
    """
    import shlex

    def field(name: str) -> str:
        # A mapping works as well as a CapacityTargets here, and a mapping
        # returns None for a missing key rather than the getattr default, so
        # both paths are handled explicitly instead of one shadowing the other.
        if isinstance(targets, dict):
            return str(targets.get(name, ""))
        return str(getattr(targets, name, ""))

    substitutions = {
        "{operator}": operator,
        "{serve_dir}": str(serve_dir),
        "{capacity_file}": str(capacity_file),
        "{max_lanes}": field("max_lanes"),
        "{max_gates}": field("max_gates"),
        "{test_pool}": field("test_pool"),
        "{typecheck_pool}": field("typecheck_pool"),
        "{gates_priority}": "1" if field("gates_priority") in ("1", "True", "true") else "0",
    }
    expanded = template
    for token, value in substitutions.items():
        expanded = expanded.replace(token, value)
    argv = shlex.split(expanded)
    return [arg for arg in argv if arg]


@dataclass
class ChildState:
    """A component's live state, serialisable so a restart resumes it."""

    name: str
    state: str = STATE_STOPPED
    pid: int | None = None
    starttime: int | None = None
    restarts: int = 0
    #: Epochs of the last ``causes`` worth of crash, for the loop detector.
    crash_epochs: list[float] = field(default_factory=list)
    #: Epoch of the last exit of any cause, for the backoff schedule.
    last_exit_epoch: float = 0.0
    last_exit_cause: str = ""
    last_exit_code: int | None = None
    #: Cause of the exit still owed an accounting decision, set when the
    #: supervisor stops a component and cleared by :meth:`_reap`. It defaults
    #: to *exit*: nobody asked for the first start, so an unasked-for exit is a
    #: crash and must consume the budget. Only an explicit
    #: ``stop_component``/``request_restart`` sets ``requested``, and that is the
    #: only thing that buys a component a free restart.
    pending_cause: str = CAUSE_EXIT
    #: When the backoff owed by the last exit is due. A restart waits for the
    #: deadline rather than sleeping inside :meth:`Supervisor.tick`.
    restart_due: float = 0.0
    adopted: bool = False
    last_event_epoch: float = 0.0
    #: Restart epochs triggered by the no-progress rule, for its own budget.
    no_progress_restarts: list[float] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "pid": self.pid,
            "starttime": self.starttime,
            "restarts": self.restarts,
            "crash_epochs": list(self.crash_epochs[-_MAX_CRASH_HISTORY:]),
            "last_exit_epoch": self.last_exit_epoch,
            "last_exit_cause": self.last_exit_cause,
            "last_exit_code": self.last_exit_code,
            "pending_cause": self.pending_cause,
            "restart_due": self.restart_due,
            "adopted": self.adopted,
            "last_event_epoch": self.last_event_epoch,
            "no_progress_restarts": list(self.no_progress_restarts[-_MAX_CRASH_HISTORY:]),
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ChildState:
        def floats(key: str) -> list[float]:
            value = raw.get(key)
            if not isinstance(value, list):
                return []
            return [float(v) for v in value if isinstance(v, int | float)]

        pid = raw.get("pid")
        starttime = raw.get("starttime")
        return cls(
            name=str(raw.get("name") or ""),
            state=str(raw.get("state") or STATE_STOPPED),
            pid=int(pid) if isinstance(pid, int) else None,
            starttime=int(starttime) if isinstance(starttime, int) else None,
            restarts=int(raw.get("restarts") or 0),
            crash_epochs=floats("crash_epochs"),
            last_exit_epoch=float(raw.get("last_exit_epoch") or 0.0),
            last_exit_cause=str(raw.get("last_exit_cause") or ""),
            last_exit_code=(
                int(raw["last_exit_code"]) if isinstance(raw.get("last_exit_code"), int) else None
            ),
            pending_cause=str(raw.get("pending_cause") or CAUSE_EXIT),
            restart_due=float(raw.get("restart_due") or 0.0),
            adopted=bool(raw.get("adopted")),
            last_event_epoch=float(raw.get("last_event_epoch") or 0.0),
            no_progress_restarts=floats("no_progress_restarts"),
            message=str(raw.get("message") or ""),
        )

    @property
    def identity(self) -> ProcIdentity | None:
        if self.pid is None or self.starttime is None:
            return None
        return ProcIdentity(pid=self.pid, starttime=self.starttime)

    def crashes_in_window(self, now: float, window_s: float) -> int:
        return sum(1 for epoch in self.crash_epochs if now - epoch <= window_s)


class Supervisor:
    """Owns the component processes for one operator."""

    def __init__(
        self,
        operator: str,
        config: ServeConfig,
        *,
        clock: Clock | None = None,
        proc_root: Path | None = None,
    ) -> None:
        self.operator = operator
        self.config = config
        self.clock = clock or SystemClock()
        self.proc_root = proc_root or Path("/proc")
        ensure_serve_dir(operator)
        self.children: dict[str, ChildState] = {}
        self._procs: dict[str, subprocess.Popen[bytes]] = {}
        self._handles: dict[str, Any] = {}
        #: Components this supervisor adopted rather than spawned. They have no
        #: Popen, so they are tracked here and stopped by fingerprint.
        self._adopted: dict[str, ProcIdentity] = {}
        self._stopping = False
        self._restore()

    # ------------------------------------------------------------------ state

    @property
    def state_file(self) -> Path:
        from agent_fleet.serve.paths import state_path

        return state_path(self.operator)

    def _restore(self) -> None:
        """Adopt persisted state; never adopts a pid that is not still ours."""
        payload: dict[str, Any] = read_json(self.state_file) or {}
        children = payload.get("children")
        if isinstance(children, dict):
            for name, raw in children.items():
                if isinstance(raw, dict):
                    self.children[str(name)] = ChildState.from_dict(dict(raw))

    def save(self) -> None:
        write_json_atomic(
            self.state_file,
            {
                "operator": self.operator,
                "updated_epoch": self.clock.time(),
                "children": {name: c.to_dict() for name, c in self.children.items()},
            },
        )

    # ------------------------------------------------------------------ spawn

    def _write_pidfile(self, name: str, state: ChildState) -> None:
        write_json_atomic(
            component_pid_path(self.operator, name),
            {
                "component": name,
                "operator": self.operator,
                "pid": state.pid,
                "starttime": state.starttime,
                "updated_epoch": self.clock.time(),
            },
        )

    def _clear_pidfile(self, name: str) -> None:
        with suppress(OSError):
            component_pid_path(self.operator, name).unlink(missing_ok=True)

    def adopt(self, name: str) -> bool:
        """Attach to an already-running component. True when one was adopted.

        A pid file alone proves nothing. The fingerprint has to match, because
        the recorded pid may have been recycled by an unrelated process between
        the previous supervisor's exit and this one reading the file — and
        "adopting" a stranger's pid would mean supervising it forever, or
        eventually killing it.
        """
        from agent_fleet.serve.paths import component_pid_path as pid_path

        payload = read_json(pid_path(self.operator, name))
        if not payload:
            return False
        pid = payload.get("pid")
        starttime = payload.get("starttime")
        if not isinstance(pid, int) or not isinstance(starttime, int):
            return False
        if starttime_fingerprint(pid, proc_root=self.proc_root) != starttime:
            return False
        if not pid_alive(pid, proc_root=self.proc_root):
            return False
        state = self.children.setdefault(name, ChildState(name=name))
        state.pid = pid
        state.starttime = starttime
        state.state = STATE_RUNNING
        state.adopted = True
        state.last_event_epoch = self.clock.time()
        self._adopted[name] = ProcIdentity(pid=pid, starttime=starttime)
        emit_serve_event(
            self.operator,
            "serve.component.adopted",
            data={"component": name, "pid": pid},
        )
        return True

    def start(self, name: str, *, cause: str = CAUSE_EXIT) -> bool:
        """Spawn a component, or adopt it if it is already running.

        The adoption check runs first and unconditionally, which is what makes
        a serve restart safe: calling ``start`` twice can never produce two
        processes for the same role.
        """
        spec = self.config.component(name)
        if not spec.enabled:
            return False
        if self.adopt(name):
            return True

        state = self.children.setdefault(name, ChildState(name=name))
        if state.state == STATE_CRASH_LOOPING:
            return False

        argv = expand_command(
            spec.command or "",
            operator=self.operator,
            serve_dir=self._serve_dir(),
            capacity_file=self._capacity_file(),
            targets=self._targets_for(),
        )
        if not argv:
            emit_serve_event(
                self.operator,
                "serve.component.empty_command",
                level="error",
                data={"component": name, "command": spec.command},
            )
            return False

        log_path = component_log_path(self.operator, name)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = log_path.open("ab")
            proc = subprocess.Popen(
                argv,
                stdout=handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                # Own session => the child is its own group leader, so
                # pgid == pid and group termination cannot reach anything serve
                # did not spawn.
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            handle.close()
            # A component that cannot even be exec'd is the worst kind of crash
            # and has to run on the same budget as any other, or the ensure-
            # running loop retries a doomed spawn once per tick forever.
            state.restarts += 1
            self._note_crash(name, cause=CAUSE_CRASH, code=None, message=str(exc))
            emit_serve_event(
                self.operator,
                "serve.component.spawn_failed",
                level="error",
                data={"component": name, "error": str(exc), "argv": argv},
            )
            if self._crash_looping(name):
                self._enter_crash_loop(name, spec)
            return False

        fingerprint = starttime_fingerprint(proc.pid, proc_root=self.proc_root)
        state.pid = proc.pid
        state.starttime = fingerprint
        state.state = STATE_RUNNING
        state.adopted = False
        state.restarts += 1
        state.pending_cause = cause
        state.restart_due = 0.0
        state.last_event_epoch = self.clock.time()
        self._procs[name] = proc
        self._adopted.pop(name, None)
        self._handles[name] = handle
        self._write_pidfile(name, state)
        emit_serve_event(
            self.operator,
            "serve.component.started",
            data={"component": name, "pid": proc.pid, "restarts": state.restarts, "cause": cause},
        )
        return True

    def _serve_dir(self) -> Path:
        from agent_fleet.serve.paths import serve_dir

        return serve_dir(self.operator)

    def _capacity_file(self) -> Path:
        from agent_fleet.serve.paths import capacity_path

        return capacity_path(self.operator)

    def _targets_for(self) -> dict[str, Any] | None:
        """The live capacity targets, projected into a command template.

        Read from the capacity file rather than from the controller in memory,
        so a command spawned by serve and one spawned by hand agree on what the
        targets are. ``None`` when nothing has been published yet.
        """
        from agent_fleet.serve.capacity import read_capacity

        payload = read_capacity(self._capacity_file())
        if not payload:
            return None
        targets = payload.get("targets")
        return targets if isinstance(targets, dict) else None

    # ------------------------------------------------------------------- exits

    def _note_crash(self, name: str, *, cause: str, code: int | None, message: str = "") -> None:
        state = self.children.setdefault(name, ChildState(name=name))
        now = self.clock.time()
        state.last_exit_epoch = now
        state.last_exit_cause = cause
        state.last_exit_code = code
        state.message = message
        if cause == CAUSE_CRASH:
            state.crash_epochs.append(now)
            if len(state.crash_epochs) > _MAX_CRASH_HISTORY:
                del state.crash_epochs[:-_MAX_CRASH_HISTORY]

    def _reap(self) -> list[tuple[str, int | None]]:
        """Reap finished children. Returns ``(name, exit_code)`` per exit.

        Reaping *before* judging liveness is the important part. An unreaped
        child sits in ``/proc`` as a zombie looking exactly like a live process,
        and a supervisor that trusts ``/proc`` alone will report a corpse as
        running and skip the restart it owes.
        """
        exits: list[tuple[str, int | None]] = []
        for name, proc in list(self._procs.items()):
            code = proc.poll()
            if code is None:
                continue
            del self._procs[name]
            handle = self._handles.pop(name, None)
            if handle is not None:
                with suppress(OSError):
                    handle.close()
            state = self.children.get(name)
            cause = state.pending_cause if state else CAUSE_CRASH
            if state is not None:
                state.pending_cause = CAUSE_EXIT
            # A non-zero exit is a crash whatever we asked for. So is a clean one
            # nobody asked for: a component that quietly exits on its own is
            # exactly the failure the crash budget is meant to catch, and letting
            # it through is what made the detector unreachable for it.
            effective = CAUSE_CRASH if code != 0 or cause not in REQUESTED_CAUSES else cause
            self._note_crash(name, cause=effective, code=code)
            exits.append((name, code))
        return exits

    def _crash_looping(self, name: str) -> bool:
        spec = self.config.component(name)
        state = self.children.get(name)
        if state is None:
            return False
        window_s = max(1.0, float(spec.crash_window_minutes) * 60.0)
        return state.crashes_in_window(self.clock.time(), window_s) >= spec.crash_threshold

    def _enter_crash_loop(self, name: str, spec: ComponentSpec) -> None:
        """Spend the crash budget: stop restarting, alert, leave the rest alone.

        One broken component must not take the pipeline down, so this only
        touches *name* — the other components keep running and the operator is
        told which one to look at.
        """
        state = self.children.get(name)
        if state is None:
            return
        state.state = STATE_CRASH_LOOPING
        state.message = (
            f"{len(state.crash_epochs)} crashes in "
            f"{spec.crash_window_minutes}m (threshold {spec.crash_threshold}); "
            f"not restarting"
        )
        alert(
            self.operator,
            "serve.component.crash_loop",
            state.message,
            component=name,
            exit_code=state.last_exit_code,
            restarts=state.restarts,
            crash_threshold=spec.crash_threshold,
            crash_window_minutes=spec.crash_window_minutes,
        )

    def backoff_for(self, name: str) -> float:
        """Exponential backoff from the restart count, capped by config.

        Computed from the count rather than accumulated, so it is a pure
        function of state: a supervisor that restarts mid-schedule resumes the
        same schedule instead of resetting to zero and hammering a component
        that is failing immediately.
        """
        spec = self.config.component(name)
        state = self.children.get(name)
        attempts = max(0, (state.restarts if state else 1) - 1)
        initial = max(0.0, float(spec.backoff_initial_s))
        ceiling = max(initial, float(spec.backoff_max_s))
        return min(ceiling, initial * (2**attempts))

    def tick(self) -> None:
        """One supervisor pass: reap, evaluate, restart what owes a restart."""
        if self._stopping:
            return
        for name, code in self._reap():
            spec = self.config.component(name)
            state = self.children.get(name)
            if state is None:
                continue
            self._clear_pidfile(name)
            state.pid = None
            state.starttime = None
            state.adopted = False
            emit_serve_event(
                self.operator,
                "serve.component.exited",
                data={
                    "component": name,
                    "exit_code": code,
                    "cause": state.last_exit_cause,
                    "restarts": state.restarts,
                },
            )

            # The backoff becomes a deadline rather than a sleep. Sleeping here
            # would park the whole loop behind one crashing component: nothing
            # else would be reaped, `serve status` would go stale, and a SIGTERM
            # could not be acted on until the sleep returned. A zero delay is
            # paid now because it costs nothing and keeps a clean exit promptly
            # restarted, as the module promises.
            delay = self.backoff_for(name)
            if delay > 0:
                state.state = STATE_BACKOFF
                state.restart_due = self.clock.time() + delay
                state.message = f"restarting in {delay:.0f}s (cause {state.last_exit_cause})"
            else:
                self._restart(name)

            if self._crash_looping(name):
                self._enter_crash_loop(name, spec)

        self._ensure_running()

    def _ensure_running(self) -> None:
        """Start anything that is enabled, not crash-looping, and not ours.

        A component in its backoff is left alone until its deadline passes, so
        a failing component cannot be retried once per tick regardless of how
        long its backoff is.
        """
        now = self.clock.time()
        for spec in self.config.enabled_components:
            name = spec.name
            if name in self._procs or name in self._adopted:
                continue
            state = self.children.get(name)
            if state is None:
                self.start(name)
                continue
            if state.state == STATE_CRASH_LOOPING or self._is_ours(name):
                continue
            if state.restart_due > now:
                continue
            self._restart(name)

    def _restart(self, name: str) -> bool:
        """Spawn a replacement for a component that is owed one.

        The old child is reaped and gone before this runs, so ``start`` cannot
        put two live copies of one role on the box — the double-dispatch failure
        the whole module is built to prevent.
        """
        state = self.children.get(name)
        cause = (state.last_exit_cause if state else "") or CAUSE_EXIT
        if not self.start(name, cause=cause):
            return False
        state = self.children.get(name)
        if state is not None:
            state.pending_cause = CAUSE_EXIT
        return True

    def _is_ours(self, name: str) -> bool:
        """True when the recorded pid is still the process we started.

        Fingerprint-gated for the same reason every kill in this package is: a
        bare ``pid_alive`` on a recorded pid accepts a recycled one, so a
        stranger's process would suppress this component's start for good while
        ``serve status`` reported the stranger as ours.
        """
        state = self.children.get(name)
        if state is None or state.identity is None:
            return False
        return state.identity.matches(proc_root=self.proc_root)

    def request_restart(self, name: str, *, reason: str) -> bool:
        """The watchdog's "this component is wedged" path.

        Tagged ``requested`` so the restart it causes does not consume the
        crash budget: a component that had to be restarted for making no
        progress three times has a real problem, but it is not crash-looping,
        and treating the two the same would stop restarting it and hide the
        real problem behind a crash-loop alert.
        """
        state = self.children.setdefault(name, ChildState(name=name))
        if state.state == STATE_CRASH_LOOPING:
            return False
        emit_serve_event(
            self.operator,
            "serve.component.restart_requested",
            level="warning",
            data={"component": name, "reason": reason},
        )
        self.stop_component(name, cause=CAUSE_REQUESTED)
        state.no_progress_restarts.append(self.clock.time())
        return self.start(name, cause=CAUSE_REQUESTED)

    def stop_component(
        self,
        name: str,
        *,
        cause: str = CAUSE_REQUESTED,
        grace_s: float = RESTART_KILL_GRACE_S,
    ) -> bool:
        """Stop a component's group and do not return until it is gone.

        TERM, wait out the grace, then KILL the group. A component that ignores
        or blocks SIGTERM used to survive this, and because the next ``start``
        overwrote the entry in ``_procs`` the old process became invisible: two
        live copies of one role, and one orphan per watchdog restart. *grace_s*
        is zero on the restart path, where a copy that will not go is killed
        outright rather than left to fight the replacement.
        """
        state = self.children.get(name)
        if state is None:
            return False
        state.pending_cause = cause
        identity = self._adopted.pop(name, None) or state.identity
        proc = self._procs.get(name)
        if proc is not None and proc.poll() is None:
            terminate_group(identity, proc_root=self.proc_root)
        self._clear_pidfile(name)
        state.state = STATE_STOPPED
        state.pid = None
        state.starttime = None
        return self._await_gone(proc, identity, grace_s=grace_s)

    def _await_gone(
        self,
        proc: subprocess.Popen[bytes] | None,
        identity: ProcIdentity | None,
        *,
        grace_s: float,
    ) -> bool:
        """Block until a component is dead, escalating to a group KILL, and reap it.

        A child of this supervisor can only really be reaped through its Popen,
        so the wait is a poll rather than a ``/proc`` glance: an unreaped child
        sits in ``/proc`` as a zombie that looks exactly like a live process.
        """
        deadline = self.clock.monotonic() + max(0.0, grace_s)
        while True:
            if proc is not None and proc.poll() is not None:
                return True
            if identity is None or not pid_alive(identity.pid, proc_root=self.proc_root):
                break
            if self.clock.monotonic() >= deadline:
                break
            time.sleep(0.02)
        escalate_kill_group(identity, proc_root=self.proc_root)
        if proc is None:
            return not pid_alive(identity.pid, proc_root=self.proc_root) if identity else True
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired, OSError:
            return False
        return True

    def shutdown(self) -> None:
        """Stop every component, in reverse start order, then persist state.

        Children are signalled as groups, never as bare pids, so a component that
        spawned its own workers takes them with it instead of leaving them
        running against a serve that has gone away. A component this supervisor
        *adopted* is stopped too: re-attaching to a process and then abandoning
        it is how a component outlives every serve that ever watched it. The
        grace is spent once and the KILL goes to whatever survived it, so
        nothing depends on a child's cooperation to be stopped.
        """
        self._stopping = True
        grace = max(0.1, self.config.shutdown_grace_s)
        names = [*self._procs, *self._adopted]
        identities: dict[str, ProcIdentity | None] = {}
        for name in reversed(names):
            state = self.children.get(name)
            identities[name] = self._adopted.get(name) or (state.identity if state else None)
            self.stop_component(name, grace_s=RESTART_KILL_GRACE_S)
        for name in reversed(names):
            self._await_gone(self._procs.get(name), identities[name], grace_s=grace)
            state = self.children.get(name)
            if state is not None:
                # Shutdown ends the accounting. Nothing is going to be restarted,
                # so a component stopped here is not waiting on a cause to be
                # judged, and the next supervisor must not inherit that.
                state.pending_cause = CAUSE_EXIT
        for handle in self._handles.values():
            with suppress(OSError):
                handle.close()
        self._procs.clear()
        self._handles.clear()
        self.save()

    def reaper_signals(self) -> dict[int, Any]:
        """Signal handlers for the main loop to install.

        The handler only flips a flag. A signal handler that does work — like
        waiting on children — can deadlock against the very syscall that
        interrupted it, and on this box a wedged shutdown is how a supervisor
        becomes a zombie that the *next* supervisor then tries to adopt.
        """
        previous: dict[int, Any] = {}

        def handler(signum: int, _frame: Any) -> None:  # noqa: ANN401
            self._stopping = True
            if not previous.get(signum):
                emit_serve_event(
                    self.operator,
                    "serve.stopping",
                    data={"signal": signum},
                )

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                previous[sig] = signal.signal(sig, handler)
            except ValueError, OSError:
                continue
        return previous

    def status_rows(self) -> list[dict[str, Any]]:
        """One row per component for ``serve status``."""
        now = self.clock.time()
        rows: list[dict[str, Any]] = []
        for name, spec in sorted(self.config.components.items()):
            state = self.children.get(name) or ChildState(name=name)
            rows.append(
                {
                    "component": name,
                    "enabled": spec.enabled,
                    "state": state.state,
                    "pid": state.pid,
                    "restarts": state.restarts,
                    "adopted": state.adopted,
                    "crashes_15m": state.crashes_in_window(now, 900.0),
                    "last_exit_cause": state.last_exit_cause,
                    "last_exit_code": state.last_exit_code,
                    "message": state.message,
                }
            )
        return rows


def install_pdeathsig() -> bool:
    """Ask the kernel to SIGTERM this process when its parent dies.

    Linux-only, via ctypes, and best-effort: a supervisor SIGKILLed would
    otherwise leave its three components running, holding worktrees and gate
    slots, with nothing left to reap them. Returns False when the prctl is
    unavailable rather than raising, because a supervisor that refuses to start
    on an exotic platform is worse than one that starts and accepts the risk.
    """
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        PR_SET_PDEATHSIG = 1
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    except OSError, AttributeError, ImportError:
        return False
    return True


__all__ = [
    "CAUSE_CAPACITY",
    "CAUSE_CRASH",
    "CAUSE_EXIT",
    "CAUSE_REQUESTED",
    "RESTART_KILL_GRACE_S",
    "STATE_BACKOFF",
    "STATE_CRASH_LOOPING",
    "STATE_RUNNING",
    "STATE_STARTING",
    "STATE_STOPPED",
    "ChildState",
    "Supervisor",
    "expand_command",
    "install_pdeathsig",
]
