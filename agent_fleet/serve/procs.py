"""Exact-PID process identity and termination.

This module is the reason :mod:`agent_fleet.serve` can self-heal safely. The
machine runs dozens of unrelated agents, and the bash watchdog this replaces
killed processes by scanning ``ps`` for argument patterns — which on this box
means it would eventually take out a colleague's agent, because every agent in
the fleet runs the same engine with a different lane name.

Two rules, both load-bearing:

**Never match by name or pattern.** There is no ``pkill``-shaped code here, and
:func:`terminate` will only ever signal a pid the caller has already proven it
owns (see :func:`owns`).

**A pid is not an identity.** Linux recycles pids, so a recorded pid can name a
completely different process by the time the watchdog looks. Every pid serve
records is therefore paired with its start-time fingerprint
(``/proc/<pid>/stat`` field 22), and :func:`terminate` refuses to signal unless
the live process still carries that fingerprint. A pid that cannot be
fingerprinted is never signalled at all.

Group termination is used only when serve itself created the group — children
are spawned with ``start_new_session=True``, so the child's pid *is* its pgid
and the group cannot contain anything serve did not put there.
"""

from __future__ import annotations

import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

#: How long a TERM is given to land before the group is KILLed.
DEFAULT_GRACE_S = 5.0


def starttime_fingerprint(pid: int, *, proc_root: Path = Path("/proc")) -> int | None:
    """Linux ``/proc/<pid>/stat`` field 22, or ``None`` when unavailable.

    Field 2 (``comm``) is space- and paren-containing, so the parse splits on
    the *last* ``)``: after ``comm`` field 3 is ``state``, making field 22 index
    19. *proc_root* is injectable so tests can fabricate ``/proc`` trees.
    """
    if pid <= 0:
        return None
    try:
        data = (proc_root / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    close = data.rfind(")")
    if close == -1:
        return None
    fields = data[close + 2 :].split()
    if len(fields) < 20:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


#: Process states that mean "already exited" despite a ``/proc`` entry.
_DEAD_STATES = frozenset({"Z", "X", "x"})


def process_state(pid: int, *, proc_root: Path = Path("/proc")) -> str | None:
    """The single-letter process state from ``/proc/<pid>/stat`` field 3.

    ``Z`` (zombie) and ``X``/``x`` (dead) are distinguished from the rest on
    purpose: a zombie has already exited and is only waiting to be reaped, so
    every live-looking signal sent to it is a no-op that would otherwise be
    reported as a successful termination.
    """
    if pid <= 0:
        return None
    try:
        data = (proc_root / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    close = data.rfind(")")
    if close == -1:
        return None
    fields = data[close + 2 :].split()
    return fields[0] if fields else None


def pid_alive(pid: int | None, *, proc_root: Path = Path("/proc")) -> bool:
    """True when *pid* names a live, non-zombie process.

    ``/proc/<pid>`` existing is not enough: an unreaped child stays in ``/proc``
    with state ``Z`` forever, and a supervisor that reports it alive will
    "terminate" a corpse and log a remediation that never happened. So the
    state field decides, and a pid whose ``stat`` is unreadable counts as
    alive — the process exists, serve just cannot see it, and assuming dead
    would be the more dangerous direction.
    """
    if not pid or pid <= 0:
        return False
    if not proc_root.joinpath(str(pid)).exists():
        return False
    state = process_state(pid, proc_root=proc_root)
    if state is None:
        return True
    return state not in _DEAD_STATES


def parent_pid(pid: int, *, proc_root: Path = Path("/proc")) -> int | None:
    """``/proc/<pid>/stat`` field 4 (ppid), or ``None``."""
    try:
        data = (proc_root / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    close = data.rfind(")")
    if close == -1:
        return None
    fields = data[close + 2 :].split()
    if len(fields) < 2:
        return None
    try:
        return int(fields[1])
    except ValueError:
        return None


def boot_time(pid: int, *, proc_root: Path = Path("/proc")) -> float | None:
    """Wall-clock epoch at which *pid* started, from ``/proc/<pid>/stat``.

    Field 22 is in clock ticks since boot, so the process's start epoch is
    ``/proc/stat`` btime plus those ticks. Used to age an orphan: an orphan's
    age cannot be read from an mtime the fleet wrote, because the fleet did not
    write it.
    """
    starttime = starttime_fingerprint(pid, proc_root=proc_root)
    if starttime is None:
        return None
    try:
        stat = (proc_root / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in stat.splitlines():
        if not line.startswith("btime "):
            continue
        try:
            boot = float(line.split()[1])
        except IndexError, ValueError:
            return None
        try:
            ticks = os.sysconf("SC_CLK_TCK")
        except ValueError, OSError:
            return None
        return boot + (starttime / ticks)
    return None


@dataclass(frozen=True)
class ProcIdentity:
    """A pid plus the fingerprint that proves it is still the same process."""

    pid: int
    starttime: int | None

    def matches(self, *, proc_root: Path = Path("/proc")) -> bool:
        """True when *pid* is alive and still carries this start time.

        A recorded identity with no fingerprint matches nothing. Refusing to
        act on an unfingerprintable pid is deliberate: without the fingerprint
        there is no way to tell the original process from a recycled pid, and
        signalling the wrong one is exactly the failure this module exists to
        prevent.
        """
        if self.starttime is None or not pid_alive(self.pid, proc_root=proc_root):
            return False
        return starttime_fingerprint(self.pid, proc_root=proc_root) == self.starttime

    def to_dict(self) -> dict[str, int | None]:
        return {"pid": self.pid, "starttime": self.starttime}

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> ProcIdentity | None:
        pid = raw.get("pid")
        starttime = raw.get("starttime")
        if not isinstance(pid, int) or pid <= 0:
            return None
        if not isinstance(starttime, int):
            return None
        return cls(pid=pid, starttime=starttime)


def owns(identity: ProcIdentity | None, *, proc_root: Path = Path("/proc")) -> bool:
    """True when *identity* still names the process the fleet started.

    This is the single gate every remediation passes through. ``identity is
    None`` — the fleet has no record of this pid — is False, not True.
    """
    return identity is not None and identity.matches(proc_root=proc_root)


@dataclass(frozen=True)
class TerminationResult:
    """What a termination actually did. Reported, never assumed."""

    signalled: bool
    escalated: bool = False
    skipped_reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "signalled": self.signalled,
            "escalated": self.escalated,
            "skipped_reason": self.skipped_reason,
        }


def _send(pid: int, sig: int) -> bool:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    except OSError:
        return False
    return True


def terminate(
    identity: ProcIdentity | None,
    *,
    proc_root: Path = Path("/proc"),
) -> TerminationResult:
    """TERM one fleet-owned process, by exact pid, and report what happened.

    Deliberately does *not* wait, and deliberately does not escalate to KILL.
    An earlier draft slept the grace here with a defaulted ``time.sleep``,
    which meant a watchdog tick blocked once per stale pid; a watchdog that
    blocks falls behind, which trips its own no-progress rule, which restarts
    the components it was protecting. The grace is spent once per tick by the
    caller, against its own remediation budget — see :func:`escalate_kill`.

    A skip is a normal outcome, not an error: it means the fleet either never
    recorded the pid, or the recorded fingerprint no longer matches. In both
    cases the right answer is to do nothing and let the caller record why.
    """
    if identity is None:
        return TerminationResult(signalled=False, skipped_reason="no recorded process identity")
    if identity.starttime is None:
        return TerminationResult(signalled=False, skipped_reason="recorded pid has no fingerprint")
    if not identity.matches(proc_root=proc_root):
        return TerminationResult(
            signalled=False,
            skipped_reason=f"pid {identity.pid} is gone or was recycled (fingerprint mismatch)",
        )

    if not _send(identity.pid, signal.SIGTERM):
        return TerminationResult(signalled=False, skipped_reason=f"pid {identity.pid} already gone")
    return TerminationResult(signalled=True)


def escalate_kill(
    identity: ProcIdentity | None,
    *,
    proc_root: Path = Path("/proc"),
    alive: Callable[[int], bool] | None = None,
) -> TerminationResult:
    """SIGKILL a process already TERMed by :func:`terminate`.

    Separate so the grace period is spent once per watchdog tick rather than
    once per stale pid, and so the caller can decide — against its own budget —
    whether an escalation is warranted at all.
    """
    is_alive = alive if alive is not None else (lambda pid: pid_alive(pid, proc_root=proc_root))
    if identity is None or identity.starttime is None:
        return TerminationResult(signalled=False, skipped_reason="no usable recorded identity")
    if not identity.matches(proc_root=proc_root):
        return TerminationResult(
            signalled=False,
            skipped_reason=f"pid {identity.pid} is gone or was recycled (fingerprint mismatch)",
        )
    if not is_alive(identity.pid):
        return TerminationResult(
            signalled=False, skipped_reason=f"pid {identity.pid} already exited"
        )
    if not _send(identity.pid, signal.SIGKILL):
        return TerminationResult(signalled=False, skipped_reason=f"pid {identity.pid} already gone")
    return TerminationResult(signalled=True, escalated=True)


def terminate_group(
    identity: ProcIdentity | None,
    *,
    proc_root: Path = Path("/proc"),
) -> TerminationResult:
    """TERM the process *group* led by a fleet-spawned child.

    Only valid for children serve started with ``start_new_session=True``: such
    a child is its own group leader, so its group contains nothing serve did not
    put there. The ``pgid != pid`` refusal is the check that keeps that true —
    a component that ``exec``ed into a wrapper is no longer a group leader, and
    signalling its group would reach processes outside the fleet.

    TERM only; see :func:`escalate_kill` for the escalation half.
    """
    if identity is None:
        return TerminationResult(signalled=False, skipped_reason="no recorded process identity")
    if identity.starttime is None:
        return TerminationResult(signalled=False, skipped_reason="recorded pid has no fingerprint")
    if not identity.matches(proc_root=proc_root):
        return TerminationResult(
            signalled=False,
            skipped_reason=f"pid {identity.pid} is gone or was recycled (fingerprint mismatch)",
        )

    try:
        pgid = os.getpgid(identity.pid)
    except OSError:
        return TerminationResult(
            signalled=False, skipped_reason="pid has no readable process group"
        )

    if pgid != identity.pid:
        return TerminationResult(
            signalled=False,
            skipped_reason=f"pid {identity.pid} is not a group leader; refusing group signal",
        )

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError, PermissionError, OSError:
        return TerminationResult(signalled=False, skipped_reason=f"group {pgid} already gone")
    return TerminationResult(signalled=True)


def escalate_kill_group(
    identity: ProcIdentity | None, *, proc_root: Path = Path("/proc")
) -> TerminationResult:
    """SIGKILL the group already TERMed by :func:`terminate_group`."""
    if identity is None or identity.starttime is None:
        return TerminationResult(signalled=False, skipped_reason="no usable recorded identity")
    if not identity.matches(proc_root=proc_root):
        return TerminationResult(
            signalled=False,
            skipped_reason=f"pid {identity.pid} is gone or was recycled (fingerprint mismatch)",
        )
    try:
        pgid = os.getpgid(identity.pid)
    except OSError:
        return TerminationResult(
            signalled=False, skipped_reason="pid has no readable process group"
        )
    if pgid != identity.pid:
        return TerminationResult(
            signalled=False,
            skipped_reason=f"pid {identity.pid} is not a group leader; refusing group signal",
        )
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError, PermissionError, OSError:
        return TerminationResult(signalled=False, skipped_reason=f"group {pgid} already gone")
    return TerminationResult(signalled=True, escalated=True)


__all__ = [
    "ProcIdentity",
    "TerminationResult",
    "boot_time",
    "escalate_kill",
    "escalate_kill_group",
    "owns",
    "parent_pid",
    "pid_alive",
    "process_state",
    "starttime_fingerprint",
    "terminate",
    "terminate_group",
]
