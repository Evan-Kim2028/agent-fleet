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
    from agent_fleet.serve.config import ServeConfig

#: Why a child exited. Only ``crash`` counts toward the crash-loop budget.
CAUSE_CRASH = "crash"
CAUSE_EXIT = "exit"
CAUSE_REQUESTED = "requested"
CAUSE_CAPACITY = "capacity"

#: A requested stop announced on a component that then *ignores* the TERM and is
#: group-KILLed by ``_await_exit``. Distinct from ``CAUSE_REQUESTED`` because the
#: reaped signal is SIGKILL, and a child killed by a signal did not get to
#: choose its own exit — so this one never counts as the child asking to stop.
CAUSE_REQUESTED_KILLED = "requested_killed"

#: Causes that record a stop serve asked for. An exit carrying one of these is
#: never a crash: the child is being restarted on purpose, so charging it to
#: the budget would eventually stop restarting a component that never crashed
#: and hide the real fault behind a crash-loop alert.
_REQUESTED_CAUSES = frozenset({CAUSE_REQUESTED, CAUSE_REQUESTED_KILLED})

#: The signals serve itself sends. A child killed by SIGKILL never ran its own
#: exit path, so the signalled/exit-code split is meaningless for it and the
#: recorded cause is the whole truth.
_KILLED_BY_SIGNAL = {-signal.SIGKILL, -signal.SIGINT}

#: Serve does not send SIGQUIT or SIGUSR1 to a component, so a child killed by
#: one of those died on its own. SIGTERM is the exception: ``stop_component``
#: and every watchdog group TERM go through it, which is the whole point.
_SIGNALLED_BY_SERVE = {-signal.SIGTERM}

STATE_STOPPED = "stopped"
STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_BACKOFF = "backoff"
STATE_CRASH_LOOPING = "crash_looping"

#: Cap on remembered crash epochs per component, so a long-lived supervisor's
#: state file does not grow without bound.
_MAX_CRASH_HISTORY = 50

#: How often the stop path re-checks whether a TERMed component has actually
#: exited. Short enough that a component that dies on the TERM costs almost
#: nothing, long enough that the poll is not the dominant cost of the wait.
_EXIT_POLL_INTERVAL_S = 0.05

#: How long the stop path gives a group KILL to land before reporting the
#: process as still present. A SIGKILL is delivered by the kernel, so this only
#: has to cover the reap; it is bounded so a stop can never hang the watchdog.
_KILL_SETTLE_S = 2.0


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
    #: Set when the watchdog asks for a restart, so a requested stop does not
    #: consume the crash budget.
    pending_cause: str = CAUSE_REQUESTED
    adopted: bool = False
    last_event_epoch: float = 0.0
    #: Monotonic instant before which this component must not be restarted.
    #: ``0.0`` means "not waiting". Set by a deferred backoff so the wait is
    #: carried by the serve loop's own pacing rather than by a sleep inside it.
    restart_at: float = 0.0
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
            pending_cause=str(raw.get("pending_cause") or CAUSE_REQUESTED),
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
        self._stopping = False
        #: When true, a crash backoff is *scheduled* rather than slept through.
        #: ``ServeLoop`` sets it: the backoff then rides out on the serve loop's
        #: own ``tick_seconds`` pacing, so a component waiting out a 300s backoff
        #: no longer freezes capacity publication and the watchdog behind it. A
        #: supervisor driven directly (``Supervisor`` used as a library, or a
        #: test) leaves it false and paces itself, so nothing waits on a loop
        #: that is not there to release it.
        self.defer_restarts = False
        self._restore()

    # ------------------------------------------------------------------ state

    @property
    def stopping(self) -> bool:
        """True once SIGTERM/SIGINT has been seen, or shutdown has begun.

        The signal handler only sets the flag; the serve loop is what reads it
        and returns, so the flag is only useful if callers outside this class
        can see it too.
        """
        return self._stopping

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
        emit_serve_event(
            self.operator,
            "serve.component.adopted",
            data={"component": name, "pid": pid},
        )
        return True

    def start(self, name: str, *, cause: str = CAUSE_REQUESTED) -> bool:
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
        # Bound before the try, not by the first statement inside it: an
        # unopenable log (a directory in its place, ENOSPC on a full volume)
        # raises out of ``open`` itself, and the handler below still has to run
        # to record the failure. An unbound ``handle`` turned that into an
        # UnboundLocalError that escaped ``ServeLoop.run``'s finally, so
        # children were left running, state was never saved and the pid file
        # was left stale for the next serve to try to adopt.
        handle: Any = None
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
            if handle is not None:
                handle.close()
            self._note_crash(name, cause=CAUSE_CRASH, code=None, message=str(exc))
            emit_serve_event(
                self.operator,
                "serve.component.spawn_failed",
                level="error",
                data={"component": name, "error": str(exc), "argv": argv},
            )
            return False

        fingerprint = starttime_fingerprint(proc.pid, proc_root=self.proc_root)
        state.pid = proc.pid
        state.starttime = fingerprint
        state.state = STATE_RUNNING
        state.adopted = False
        state.restarts += 1
        state.pending_cause = cause
        state.last_event_epoch = self.clock.time()
        self._procs[name] = proc
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
            # A death serve asked for is not a crash, whatever the exit code
            # says. A watchdog group TERM arrives as exit code -15, and reading
            # it as "non-zero, so crash" charged every one of them against the
            # crash budget — so a component the watchdog merely restarted was
            # eventually marked crash_looping and stopped for a fault it never
            # had, with the real one hidden behind a crash-loop alert.
            #
            # The signal is the discriminator, not the tag. ``start`` defaults
            # its cause to ``requested``, so trusting the label alone would also
            # excuse a component that fell over on its own between spawns. What
            # separates the two is whether serve sent the signal: a TERM it
            # sent is a requested stop, a signal it never sends is a crash, and
            # a child that exits by itself after being asked to stop chose that
            # code, so it still counts.
            if cause in _REQUESTED_CAUSES and code is not None:
                if code in _KILLED_BY_SIGNAL:
                    effective = CAUSE_REQUESTED_KILLED
                elif code == 0 or code in _SIGNALLED_BY_SERVE:
                    effective = cause
                else:
                    effective = CAUSE_CRASH
            else:
                effective = CAUSE_CRASH if code != 0 else cause
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
        """One supervisor pass: reap, evaluate, restart what owes a restart.

        Never blocks on a backoff. A component that owes a restart is given a
        monotonic ``restart_at`` and left in ``backoff``; a later tick — paced by
        the serve loop — picks it up once the deadline has passed. Sleeping the
        delay here instead froze the *whole* serve loop for its full length, so
        with the shipped defaults (5s growing to 300s) a crash-looping component
        stopped capacity publication and every watchdog rule behind it: targets
        went unpublished, so a command template spawned against a stale file, and
        remediations simply did not run.

        :meth:`pending_restarts` is the bound on how much time is still owed,
        so a caller that paces itself can sleep the residual instead of
        hard-coding a tick.
        """
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

            if self._crash_looping(name):
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
                    exit_code=code,
                    restarts=state.restarts,
                    crash_threshold=spec.crash_threshold,
                    crash_window_minutes=spec.crash_window_minutes,
                )
                continue

            delay = self.backoff_for(name)
            state.state = STATE_BACKOFF
            state.message = f"restarting in {delay:.0f}s (cause {state.last_exit_cause})"
            state.restart_at = self.clock.monotonic() + delay
            if delay > 0 and not self.defer_restarts:
                # Self-paced: nothing outside this call is going to bring the
                # next tick, so the wait is taken here. ``FakeClock`` makes this
                # free, and the restart is then due within this same pass.
                self.clock.sleep(delay)
            if self._stopping:
                return

        for name in list(self.children):
            if self._stopping:
                return
            state = self.children[name]
            if state.state != STATE_BACKOFF or self._crash_looping(name):
                continue
            if self.clock.monotonic() < state.restart_at:
                continue
            state.restart_at = 0.0
            self.start(name, cause=state.last_exit_cause or CAUSE_EXIT)

        for spec in self.config.enabled_components:
            if spec.name not in self._procs and not pid_alive(
                (self.children.get(spec.name) or ChildState(name=spec.name)).pid or -1,
                proc_root=self.proc_root,
            ):
                state = self.children.get(spec.name)
                if state is not None and state.state in (
                    STATE_CRASH_LOOPING,
                    STATE_BACKOFF,
                ):
                    # A component already serving its crash backoff is owed a
                    # restart by the deadline loop above, not by this one —
                    # starting it here would restart it immediately and turn the
                    # exponential schedule into a hot loop.
                    continue
                self.start(spec.name)

    def pending_restarts(self) -> float:
        """Seconds still owed to components serving a crash backoff.

        The residual, so a caller that paces itself can sleep what is left
        instead of guessing at the largest configured backoff.
        """
        now = self.clock.monotonic()
        return max(
            (max(0.0, s.restart_at - now) for s in self.children.values() if s.restart_at > 0.0),
            default=0.0,
        )

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

    def stop_component(self, name: str, *, cause: str = CAUSE_REQUESTED) -> bool:
        """Stop a component, by recorded fingerprint only, and wait for it to go.

        Signalling is driven from the *recorded identity*, never from whether a
        ``Popen`` handle happens to be in :attr:`_procs`. An adopted component
        — one this supervisor re-attached to at boot rather than spawned — is
        in exactly that state: it is in :attr:`children` and it is running, but
        it has no handle here. Gating the TERM on the handle meant an adopted
        component was never signalled, its pid file was cleared anyway, and the
        follow-up ``start`` then re-checked :meth:`adopt` against a pid file
        that no longer existed — so the fingerprint proof never ran and a second
        process was spawned for the same role. Re-attach, never double-start.

        The wait is what makes the TERM worth sending. ``stop_component`` is
        followed by an immediate ``start``, so returning while the old process
        is still alive leaves the wedged one running alongside its replacement:
        a component that traps SIGTERM would leak one unkillable process per
        watchdog retry, forever, because nulling the identity here is exactly
        what makes the TERM impossible to escalate. So the identity is held
        until the process is confirmed gone, and a group KILL finishes the job
        when the grace runs out. The grace is spent once, per component, and is
        bounded by config.
        """
        state = self.children.get(name)
        if state is None:
            return False
        state.pending_cause = cause
        identity = state.identity
        proc = self._procs.get(name)
        if proc is not None and proc.poll() is not None:
            # Already exited; _reap will account for it. Nothing to signal.
            identity = None
        if identity is not None:
            terminate_group(identity, proc_root=self.proc_root)
            self._await_exit(name, identity, proc)
        self._clear_pidfile(name)
        state.state = STATE_STOPPED
        state.pid = None
        state.starttime = None
        return True

    def _await_exit(
        self,
        name: str,
        identity: ProcIdentity,
        proc: subprocess.Popen[bytes] | None,
    ) -> bool:
        """Wait out the TERM grace, then group-KILL whatever is still there.

        Returns True when the process is gone by the time this returns, so the
        caller knows a replacement ``start`` is safe. Polls on a short interval
        rather than sleeping the whole grace, so a component that exits on the
        TERM does not cost the caller the full wait.
        """
        grace = max(0.0, float(self.config.shutdown_grace_s))
        deadline = self.clock.monotonic() + grace
        while self.clock.monotonic() < deadline:
            if self._exited(identity, proc):
                return True
            self.clock.sleep(_EXIT_POLL_INTERVAL_S)
        if self._exited(identity, proc):
            return True
        # Still there after the grace: the TERM was ignored or trapped. The
        # identity is still intact precisely because stop_component has not
        # nulled it yet, so the escalation can still prove it owns the pid.
        killed = escalate_kill_group(identity, proc_root=self.proc_root)
        if killed.signalled:
            emit_serve_event(
                self.operator,
                "serve.component.kill_escalated",
                level="warning",
                data={"component": name, "pid": identity.pid},
            )
        # The KILL is not instantaneous; give it a bounded moment to land so
        # the caller does not spawn a replacement while the corpse is still
        # holding the group's resources.
        kill_deadline = self.clock.monotonic() + _KILL_SETTLE_S
        while self.clock.monotonic() < kill_deadline:
            if self._exited(identity, proc):
                return True
            self.clock.sleep(_EXIT_POLL_INTERVAL_S)
        return self._exited(identity, proc)

    def _exited(
        self,
        identity: ProcIdentity,
        proc: subprocess.Popen[bytes] | None,
    ) -> bool:
        """True once the process is gone, by waitpid or by ``/proc``.

        A process this supervisor spawned can be reaped, and ``proc.poll`` is
        the authoritative answer for it. An adopted one cannot — it is not our
        child, so there is nothing to waitpid — and is judged by whether its
        fingerprint still resolves in ``/proc``.
        """
        if proc is not None:
            return proc.poll() is not None
        return not identity.matches(proc_root=self.proc_root)

    def shutdown(self) -> None:
        """Stop every child, in reverse start order, then persist state.

        Children are TERMed as groups, never as bare pids, so a component that
        spawned its own workers takes them with it instead of leaving them
        running against a serve that has gone away.
        """
        self._stopping = True
        grace = max(0.1, self.config.shutdown_grace_s)
        for name in reversed(list(self._procs)):
            self.stop_component(name)
        for proc in self._procs.values():
            with suppress(subprocess.TimeoutExpired, OSError):
                proc.wait(timeout=grace)
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
    "CAUSE_REQUESTED_KILLED",
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
