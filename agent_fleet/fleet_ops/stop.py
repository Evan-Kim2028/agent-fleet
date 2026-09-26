"""Stop exactly one lane — by recorded process group, never by name pattern.

The bash drivers reached for ``pkill -f`` and ``pgrep -x devin`` + a cwd
comparison. Both are wrong for a two-operator setup: ``pkill -f`` matches any
process whose command line merely *contains* the pattern, so a sibling
operator's lane (same binary, same worktree layout, different lane) can be
killed along with the intended one. This module never matches on a command
line at all.

Instead every lane run records, at launch time, the triple
``(pid, pgid, starttime)`` where ``starttime`` is ``/proc/<pid>/stat`` field 22.
To stop a lane we:

1. Refuse outright if the recorded pgid is *our own* process group — a lane
   that somehow recorded the caller's group must never be signalled.
2. Re-read ``/proc/<pid>/stat`` and require the start-time fingerprint to still
   match. A mismatch means the pid was recycled by an unrelated process, and
   signalling the (now different) pgid would hit the wrong tree. A record with
   no fingerprint at all is refused on the same reasoning: there is nothing to
   match it against, and the downside is another operator's shell.
3. ``killpg(pgid, SIGTERM)``, wait a grace period, then ``SIGKILL`` if needed.

If the process is already gone we report that rather than reporting a failure.
"""

from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_fleet.fleet_ops.registry import (
    LaneRecord,
    process_alive,
    process_starttime,
    update_record,
)

if TYPE_CHECKING:
    from collections.abc import Callable

DEFAULT_GRACE_S = 10.0

#: Exit/reason codes returned in ``StopResult.reason``.
OK_STOPPED = "stopped"
OK_ALREADY_GONE = "already_gone"
REFUSED_OWN_PGROUP = "refused_own_pgid"
REFUSED_NO_PROCESS = "refused_no_process_identity"
REFUSED_PID_REUSED = "refused_pid_reused"
REFUSED_NOT_FOUND = "refused_lane_not_found"
REFUSED_AMBIGUOUS = "refused_ambiguous_lane"


@dataclass(frozen=True)
class StopResult:
    stopped: bool
    reason: str
    pid: int | None = None
    pgid: int | None = None
    signalled: bool = False
    forced: bool = False
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "stopped": self.stopped,
            "reason": self.reason,
            "pid": self.pid,
            "pgid": self.pgid,
            "signalled": self.signalled,
            "forced": self.forced,
            "detail": self.detail,
        }


def own_pgid() -> int:
    """The process group id of the current process."""
    return os.getpgid(0)


def verify_process_identity(record: LaneRecord) -> tuple[bool, str]:
    """Check that *record*'s recorded process is still the same process.

    Returns ``(ok, reason)``. ``ok`` is True only when the pid is alive **and**
    its start-time fingerprint matches what was recorded — both halves are
    required, not one of them. A record with no fingerprint cannot be shown to
    name the process that wrote it, and the only consequence of a recycled pid is
    ``killpg`` landing on a stranger's process tree, so a fingerprint the machine
    cannot supply is a refusal rather than a gap in the check. Every writer of
    this record (the lane manager, :func:`stop_lane_by_name`) records all three
    fields together, so an absent one means a torn or hand-edited record.
    """
    if not record.pid or not record.pgid:
        return False, REFUSED_NO_PROCESS
    if not process_alive(record.pid):
        return False, OK_ALREADY_GONE
    current = process_starttime(record.pid)
    if record.starttime is None or current is None:
        return False, REFUSED_PID_REUSED
    if current != record.starttime:
        # Same pid, different birth time: the original died and the number was
        # reused. Signalling the recorded pgid could hit an unrelated tree.
        return False, REFUSED_PID_REUSED
    return True, "ok"


def stop_lane(
    record: LaneRecord,
    *,
    grace_s: float = DEFAULT_GRACE_S,
    poll_s: float = 0.2,
    own_pgid_value: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> StopResult:
    """Terminate the process group recorded for *record*. See module docstring."""
    if record.pgid is None or record.pid is None:
        return StopResult(
            stopped=False, reason=REFUSED_NO_PROCESS, detail="no recorded process identity"
        )

    if own_pgid_value is None:
        own_pgid_value = own_pgid()

    if record.pgid == own_pgid_value or record.pid == os.getpid():
        # Never signal our own group — that would kill the operator's shell.
        return StopResult(
            stopped=False,
            reason=REFUSED_OWN_PGROUP,
            pid=record.pid,
            pgid=record.pgid,
            detail="recorded pgid is the caller's own process group; refusing to signal it",
        )

    ok, reason = verify_process_identity(record)
    if not ok:
        if reason == OK_ALREADY_GONE:
            return StopResult(
                stopped=True, reason=OK_ALREADY_GONE, pid=record.pid, pgid=record.pgid
            )
        return StopResult(stopped=False, reason=reason, pid=record.pid, pgid=record.pgid)

    try:
        os.killpg(record.pgid, signal.SIGTERM)
    except ProcessLookupError:
        return StopResult(stopped=True, reason=OK_ALREADY_GONE, pid=record.pid, pgid=record.pgid)
    except PermissionError as exc:
        return StopResult(
            stopped=False,
            reason="refused_permission",
            pid=record.pid,
            pgid=record.pgid,
            detail=str(exc),
        )

    deadline = time.monotonic() + max(0.0, grace_s)
    while time.monotonic() < deadline:
        if not process_alive(record.pid):
            return StopResult(
                stopped=True, reason=OK_STOPPED, pid=record.pid, pgid=record.pgid, signalled=True
            )
        sleep(poll_s)

    # Grace expired — escalate, but re-verify identity first so we never
    # SIGKILL a recycled pid.
    ok, reason = verify_process_identity(record)
    if not ok:
        if reason == OK_ALREADY_GONE:
            return StopResult(
                stopped=True, reason=OK_STOPPED, pid=record.pid, pgid=record.pgid, signalled=True
            )
        return StopResult(
            stopped=False, reason=reason, pid=record.pid, pgid=record.pgid, signalled=True
        )

    try:
        os.killpg(record.pgid, signal.SIGKILL)
    except ProcessLookupError:
        return StopResult(
            stopped=True, reason=OK_STOPPED, pid=record.pid, pgid=record.pgid, signalled=True
        )
    return StopResult(
        stopped=True,
        reason=OK_STOPPED,
        pid=record.pid,
        pgid=record.pgid,
        signalled=True,
        forced=True,
    )


def stop_lane_by_name(
    lane: str,
    *,
    operator: str | None = None,
    grace_s: float = DEFAULT_GRACE_S,
) -> StopResult:
    """Resolve a lane by name and stop it.

    Ambiguity is a refusal, not a coin flip: two operators can legitimately own
    the same lane name, and killing the wrong one is exactly the failure this
    module exists to prevent. The caller should re-run with ``--operator``.
    """
    from agent_fleet.fleet_ops.registry import find_lane, iter_records

    record = find_lane(lane, operator=operator)
    if record is None:
        matches = [r for r in iter_records() if r.lane == lane]
        if len(matches) > 1:
            return StopResult(
                stopped=False,
                reason=REFUSED_AMBIGUOUS,
                detail=(
                    f"lane {lane!r} exists for operators "
                    f"{sorted(r.operator for r in matches)}; pass --operator"
                ),
            )
        return StopResult(stopped=False, reason=REFUSED_NOT_FOUND, detail=f"no lane {lane!r}")

    result = stop_lane(record, grace_s=grace_s)
    if result.stopped:
        update_record(
            record.operator,
            record.lane,
            state="stopped",
            pid=None,
            pgid=None,
            starttime=None,
            reason=result.reason,
            event="lane.stopped",
        )
    return result
