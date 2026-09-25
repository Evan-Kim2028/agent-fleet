"""Multi-operator lane registry.

State lives in one small JSON file per lane, plus a single append-only event
stream shared by every operator::

    ~/.agent-fleet/lanes/<operator>/<lane>.json
    ~/.agent-fleet/lanes/events.jsonl

Splitting by operator matters because two operator sessions run concurrently
against the *same repos* — ``documents-0e`` and ``documents-1d`` each own their
own lanes, and neither should be able to clobber the other's record. The event
stream is shared and append-only so a single ``lanes status`` view can show
cross-operator activity, and so the history of a lane survives a restart (the
JSON file only holds the latest snapshot).

The record also carries the process identity — pid, pgid, and a start-time
fingerprint — that :func:`stop_lane` needs in order to kill exactly one lane's
process group. See that function for why a pid alone is not enough.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_fleet.fleet_paths import agent_fleet_home

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Lane lifecycle states surfaced by ``lanes status``.
STATE_RUNNING = "running"
STATE_IDLE = "idle"
STATE_STALLED = "stalled"
STATE_ESCALATED = "escalated"
STATE_APPROVED = "approved"
STATE_MERGED = "merged"
STATE_PR_GUARANTEED = "pr_guaranteed"
STATE_STOPPED = "stopped"

KNOWN_STATES = frozenset(
    {
        STATE_RUNNING,
        STATE_IDLE,
        STATE_STALLED,
        STATE_ESCALATED,
        STATE_APPROVED,
        STATE_MERGED,
        STATE_PR_GUARANTEED,
        STATE_STOPPED,
    }
)

#: States from which no further automatic work happens.
TERMINAL_STATES = frozenset({STATE_ESCALATED, STATE_APPROVED, STATE_MERGED, STATE_STOPPED})

#: Which stage of the pipeline a lane is in. Distinct from ``state``: a lane can
#: be ``running`` in phase ``impl`` and later ``running`` in phase ``gate``, and
#: which one it is in is what tells an operator whether a lane is working or
#: waiting on a verdict.
PHASE_IMPL = "impl"
PHASE_GATE = "gate"
PHASE_FIX = "fix"
PHASE_RECHECK = "recheck"
PHASE_DONE = "done"

KNOWN_PHASES = frozenset({PHASE_IMPL, PHASE_GATE, PHASE_FIX, PHASE_RECHECK, PHASE_DONE})


def lanes_dir() -> Path:
    return agent_fleet_home() / "lanes"


def events_path() -> Path:
    return lanes_dir() / "events.jsonl"


def lane_state_path(operator: str, lane: str) -> Path:
    return lanes_dir() / operator / f"{lane}.json"


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_hhmmss() -> str:
    """Local wall-clock ``HH:MM:SS`` — the format the status-file lines use."""
    return datetime.now().strftime("%H:%M:%S")


@dataclass
class LaneRecord:
    """A lane's latest snapshot.

    ``pid``/``pgid``/``starttime`` are the process identity recorded when the
    engine was launched. ``starttime`` is Linux ``/proc/<pid>/stat`` field 22
    (process start time in clock ticks since boot) — the only cheap way to tell
    a *live* pid from a recycled one.
    """

    lane: str
    operator: str
    state: str = STATE_IDLE
    engine: str | None = None
    pr: int | None = None
    head: str | None = None
    worktree: str | None = None
    branch: str | None = None
    repo_path: str | None = None
    pid: int | None = None
    pgid: int | None = None
    starttime: int | None = None
    tool_calls: int = 0
    tool_errors: int = 0
    last_event: str | None = None
    reason: str | None = None
    #: ``owner/repo`` slug, read from the worktree's own ``origin`` remote. Shown
    #: in the status table because "which repo is this lane in" is exactly the
    #: question that a cross-operator view has to answer unambiguously.
    repo: str | None = None
    #: Pipeline stage: impl / gate / fix / recheck / done.
    phase: str = PHASE_IMPL
    #: The most recent status line written for this lane, mirrored from the
    #: status file so ``lanes status`` can show the verdict without reading a
    #: file that may live outside the registry.
    status_line: str | None = None
    started_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    # Monotonic-ish wall clock for age arithmetic, so `lanes status` can
    # compute age / idle-for without parsing timestamps.
    started_ts: float = field(default_factory=time.time)
    updated_ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "operator": self.operator,
            "state": self.state,
            "engine": self.engine,
            "pr": self.pr,
            "head": self.head,
            "worktree": self.worktree,
            "branch": self.branch,
            "repo_path": self.repo_path,
            "pid": self.pid,
            "pgid": self.pgid,
            "starttime": self.starttime,
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
            "last_event": self.last_event,
            "reason": self.reason,
            "repo": self.repo,
            "phase": self.phase,
            "status_line": self.status_line,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "started_ts": self.started_ts,
            "updated_ts": self.updated_ts,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LaneRecord:
        def _int(key: str) -> int:
            value = raw.get(key)
            return int(value) if isinstance(value, (int, float)) else 0

        def _opt_int(key: str) -> int | None:
            value = raw.get(key)
            return int(value) if isinstance(value, (int, float)) else None

        return cls(
            lane=str(raw.get("lane") or ""),
            operator=str(raw.get("operator") or ""),
            state=str(raw.get("state") or STATE_IDLE),
            engine=raw.get("engine"),
            pr=_opt_int("pr"),
            head=raw.get("head"),
            worktree=raw.get("worktree"),
            branch=raw.get("branch"),
            repo_path=raw.get("repo_path"),
            pid=_opt_int("pid"),
            pgid=_opt_int("pgid"),
            starttime=_opt_int("starttime"),
            tool_calls=_int("tool_calls"),
            tool_errors=_int("tool_errors"),
            last_event=raw.get("last_event"),
            reason=raw.get("reason"),
            repo=raw.get("repo"),
            phase=str(raw.get("phase") or PHASE_IMPL),
            status_line=raw.get("status_line"),
            started_at=str(raw.get("started_at") or now_iso()),
            updated_at=str(raw.get("updated_at") or now_iso()),
            started_ts=float(raw.get("started_ts") or time.time()),
            updated_ts=float(raw.get("updated_ts") or time.time()),
        )

    @property
    def tool_error_pct(self) -> float:
        if self.tool_calls <= 0:
            return 0.0
        return 100.0 * self.tool_errors / self.tool_calls

    def age_s(self, now: float | None = None) -> float:
        return max(0.0, (now if now is not None else time.time()) - self.started_ts)

    def idle_s(self, now: float | None = None) -> float:
        return max(0.0, (now if now is not None else time.time()) - self.updated_ts)


def save_record(record: LaneRecord) -> Path:
    """Persist *record* atomically to its lane path."""
    path = lane_state_path(record.operator, record.lane)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def load_record(operator: str, lane: str) -> LaneRecord | None:
    path = lane_state_path(operator, lane)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError, OSError:
        return None
    if not isinstance(raw, dict):
        return None
    return LaneRecord.from_dict(raw)


def update_record(operator: str, lane: str, **fields: Any) -> LaneRecord:  # noqa: ANN401
    """Load, mutate, and persist a lane record, appending an event when asked.

    Centralising the write means every state transition is timestamped and (for
    ``event=``) lands in the shared stream exactly once.
    """
    record = load_record(operator, lane) or LaneRecord(lane=lane, operator=operator)
    for key, value in fields.items():
        if value is not None or key in ("pr", "head", "pid", "pgid", "starttime", "reason"):
            setattr(record, key, value)
    record.updated_at = now_iso()
    record.updated_ts = time.time()
    save_record(record)
    event = fields.get("event")
    if event:
        append_event(operator, lane, str(event), state=record.state)
    return record


def append_event(operator: str, lane: str, event: str, **fields: Any) -> Path:  # noqa: ANN401
    """Append one line to the shared event stream. Never rewrites history."""
    path = events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": now_iso(),
        "ts_epoch": time.time(),
        "operator": operator,
        "lane": lane,
        "event": event,
        **fields,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, default=str) + "\n")
    return path


def iter_records() -> Iterator[LaneRecord]:
    """Every lane record on the machine, across all operators."""
    root = lanes_dir()
    if not root.is_dir():
        return
    for op_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for state_file in sorted(op_dir.glob("*.json")):
            try:
                raw = json.loads(state_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError, OSError:
                continue
            if isinstance(raw, dict):
                yield LaneRecord.from_dict(raw)


def read_events(operator: str | None = None, lane: str | None = None) -> list[dict[str, Any]]:
    """Read the event stream, optionally filtered by operator and/or lane."""
    path = events_path()
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if operator is not None and row.get("operator") != operator:
            continue
        if lane is not None and row.get("lane") != lane:
            continue
        rows.append(row)
    return rows


def find_lane(lane: str, operator: str | None = None) -> LaneRecord | None:
    """Locate a lane record by name, optionally constrained to one operator.

    Lane names are not guaranteed unique across operators, so an ambiguous
    lookup returns ``None`` and the caller must disambiguate — silently picking
    the first match would risk stopping the wrong operator's process.
    """
    matches = [
        r for r in iter_records() if r.lane == lane and (operator is None or r.operator == operator)
    ]
    return matches[0] if len(matches) == 1 else None


# ---------------------------------------------------------------- process identity


def process_starttime(pid: int) -> int | None:
    """Return Linux ``/proc/<pid>/stat`` field 22, or ``None`` if unavailable.

    Field 22 is the process start time in clock ticks since boot — stable for
    the life of a process, and (with the pid) a fingerprint that a recycled pid
    cannot reproduce. Field 2 (``comm``) is space- and paren-containing, so the
    parse splits after the *last* ``)``.
    """
    try:
        data = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    close = data.rfind(")")
    if close == -1:
        return None
    fields = data[close + 2 :].split()
    # After comm, field 3 is `state`, so field 22 is index 19.
    if len(fields) < 20:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
