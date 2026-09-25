"""The ``lanes status`` table — the cross-operator view.

Ports ``fbstatus``'s per-run line, but keyed on lanes rather than on raw run
names, and unified across every operator so one table answers "what is the whole
swarm doing right now".

The columns are the ones an operator actually acted on in the bash era (lane,
owner, state, age, idle-for, tool-error rate, PR) plus what documents-1d asked
for explicitly: repo, head sha9, phase, and the last status line. The last status
line matters most — it is the token ``automerge.sh`` and both operators' tooling
key off, so the table shows the verdict without anyone having to open the file.
"""

from __future__ import annotations

import time
from typing import Any

from agent_fleet.fleet_ops.registry import (
    STATE_RUNNING,
    STATE_STALLED,
    LaneRecord,
    process_alive,
)

#: (header, width) pairs. Widths are minimums; rows are not truncated so a long
#: lane name stays copy-pasteable.
COLUMNS: tuple[tuple[str, int], ...] = (
    ("LANE", 20),
    ("OPERATOR", 14),
    ("REPO", 22),
    ("STATE", 13),
    ("PHASE", 7),
    ("PR", 6),
    ("HEAD", 9),
    ("AGE", 8),
    ("IDLE-FOR", 9),
    ("TOOL-ERR%", 9),
    ("LAST-STATUS", 30),
)

_ALIGN_RIGHT = {"AGE", "IDLE-FOR", "TOOL-ERR%", "PR"}


def _fmt_duration(seconds: float) -> str:
    """Compact human duration: ``12s``, ``4m``, ``2h10m``, ``3d4h``."""
    if seconds < 0:
        seconds = 0.0
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        hours, rem = divmod(s, 3600)
        return f"{hours}h{rem // 60}m" if rem >= 60 else f"{hours}h"
    days, rem = divmod(s, 86400)
    return f"{days}d{rem // 3600}h"


def derive_state(record: LaneRecord, *, now: float | None = None) -> str:  # noqa: ARG001
    """The state to display for *record*.

    A record left in ``running`` whose process is gone is *not* running — it is
    stalled (or dead). Reporting it as running is how a lane got lost in the bash
    era: the file said running forever because nothing ever reconciled it with
    reality.
    """
    if record.state != STATE_RUNNING:
        return record.state
    if not process_alive(record.pid):
        return STATE_STALLED
    return STATE_RUNNING


def row_for(record: LaneRecord, *, now: float | None = None) -> list[str]:
    """One row's cells, in ``COLUMNS`` order."""
    now = now if now is not None else time.time()
    return [
        record.lane,
        record.operator,
        record.repo or "-",
        derive_state(record, now=now),
        record.phase or "-",
        f"#{record.pr}" if record.pr else "-",
        (record.head or "-")[:9],
        _fmt_duration(record.age_s(now)),
        _fmt_duration(record.idle_s(now)),
        f"{record.tool_error_pct:.0f}%",
        record.status_line or "-",
    ]


def render_table(
    records: list[LaneRecord],
    *,
    now: float | None = None,
    title: str | None = None,
) -> str:
    """Render the lanes table. Returns a header-only table for no records."""
    lines: list[str] = []
    if title:
        lines.append(title)
    lines.append("  ".join(h.ljust(w) for h, w in COLUMNS).rstrip())
    lines.append("  ".join("-" * w for _, w in COLUMNS))

    for record in records:
        cells: list[str] = []
        for (header, width), value in zip(COLUMNS, row_for(record, now=now), strict=True):
            cells.append(value.rjust(width) if header in _ALIGN_RIGHT else value.ljust(width))
        lines.append("  ".join(cells).rstrip())

    if not records:
        lines.append("(no lanes registered)")
    return "\n".join(lines)


def status_rows(
    *,
    operator: str | None = None,
    now: float | None = None,
) -> list[LaneRecord]:
    """Lane records for the status table, running lanes first then most recent.

    ``operator=None`` means all operators — the point of the shared registry.
    """
    from agent_fleet.fleet_ops.registry import iter_records

    now = now if now is not None else time.time()
    rows = [r for r in iter_records() if operator is None or r.operator == operator]
    rows.sort(key=lambda r: (derive_state(r, now=now) == STATE_RUNNING, -r.updated_ts))
    return rows


def status_dicts(*, operator: str | None = None, now: float | None = None) -> list[dict[str, Any]]:
    """JSON-friendly status rows, for ``--json``."""
    now = now if now is not None else time.time()
    return [
        {
            "lane": r.lane,
            "operator": r.operator,
            "repo": r.repo,
            "state": derive_state(r, now=now),
            "phase": r.phase,
            "pr": r.pr,
            "head": r.head,
            "head_sha9": (r.head or "")[:9],
            "age_s": r.age_s(now),
            "idle_s": r.idle_s(now),
            "tool_calls": r.tool_calls,
            "tool_errors": r.tool_errors,
            "tool_error_pct": r.tool_error_pct,
            "status_line": r.status_line,
            "engine": r.engine,
            "branch": r.branch,
            "worktree": r.worktree,
            "last_event": r.last_event,
            "reason": r.reason,
        }
        for r in status_rows(operator=operator, now=now)
    ]


__all__ = ["COLUMNS", "derive_state", "render_table", "row_for", "status_dicts", "status_rows"]
