"""One screen that answers "is the fleet healthy, and if not, where".

``fleet serve status`` is the thing an operator actually looks at, so it is
built to be read at a glance and to be *honest when it does not know*. The
distinguishing property of this renderer is that a failed measurement reads as
a failure:

    capacity  DEGRADED  pressure source unavailable: /sys/fs/cgroup/agents.slice
              not found under /sys/fs/cgroup (tried: ., user.slice, ...)
              lanes 1/20  gates 1/12  (held at floor)

Printing ``cpu some-avg60 0.0`` there would be worse than printing nothing. Zero
is what a broken read produces, and it is indistinguishable from an idle
machine in the same field — so a reader would conclude the box was fine at the
moment it was least able to tell.

Everything on the screen is derived, never cached: components from the
supervisor, targets and pressure from the capacity file, the board from the
transition log. That costs a few file reads and buys a screen that cannot
disagree with reality because it is not a copy of it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_fleet.serve.capacity import SIGNAL_UNKNOWN, read_capacity
from agent_fleet.serve.items import STAGES

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.serve.capacity import CapacityTargets
    from agent_fleet.serve.items import ItemBoard
    from agent_fleet.serve.supervisor import Supervisor

#: How wide the stage column renders, so the screen stays a screen.
_STAGE_WIDTH = 10


def _fmt_age(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.0f}h"


def status_snapshot(
    operator: str,
    supervisor: Supervisor,
    board: ItemBoard,
    *,
    window_hours: float = 1.0,
    capacity_file: Path | None = None,
) -> dict[str, Any]:
    """The whole status screen as one dict. The text render is a view of this.

    ``--json`` prints this verbatim, so the JSON and the text screen can never
    disagree — there is one source, and the text is a projection of it.
    """
    path = capacity_file or _default_capacity_file(operator)
    document = read_capacity(path) if path is not None else None
    targets_doc = (document or {}).get("targets") if isinstance(document, dict) else None
    pressure = (document or {}).get("pressure") if isinstance(document, dict) else None
    targets: CapacityTargets | None = None
    if isinstance(targets_doc, dict):
        targets = _targets_from_doc(targets_doc)

    stage_stats = board.stage_stats(targets=targets, window_hours=window_hours)
    depths = board.depth()
    supervisor_state = _supervisor_state(supervisor)
    pressure_ok = bool(isinstance(pressure, dict) and pressure.get("ok"))

    return {
        "operator": operator,
        "supervisor": supervisor_state,
        "capacity": {
            "available": targets is not None,
            "targets": targets.to_dict() if targets is not None else None,
            "pressure_ok": pressure_ok,
            "pressure_error": (pressure or {}).get("error", "")
            if isinstance(pressure, dict)
            else "",
            "pressure_path": (pressure or {}).get("path", "") if isinstance(pressure, dict) else "",
            "cpu_some_avg60": _cpu_avg60(pressure),
            "memory_ratio": (pressure or {}).get("memory_ratio")
            if isinstance(pressure, dict)
            else None,
            "gates_priority": bool(targets.gates_priority) if targets is not None else False,
            "updated_epoch": (document or {}).get("updated_epoch")
            if isinstance(document, dict)
            else None,
        },
        "stages": [
            {
                "stage": stat.stage,
                "depth": stat.depth,
                "oldest_item": stat.oldest.item_id if stat.oldest else None,
                "oldest_age_s": round(stat.oldest_age_s, 1),
                "wait_reason": stat.wait_reason,
                "per_hour": round(stat.per_hour, 2),
            }
            for stat in stage_stats
        ],
        "depth": depths,
        "queue_depth": sum(depths.get(stage, 0) for stage in STAGES if stage not in ("merged",)),
        "window_hours": window_hours,
    }


def _default_capacity_file(operator: str) -> Path:
    from agent_fleet.serve.paths import capacity_path

    return capacity_path(operator)


def _targets_from_doc(payload: dict[str, Any]) -> CapacityTargets | None:
    from agent_fleet.serve.capacity import CapacityTargets

    def num(key: str) -> int:
        value = payload.get(key)
        return int(value) if isinstance(value, int) else 0

    return CapacityTargets(
        max_lanes=num("max_lanes"),
        max_gates=num("max_gates"),
        test_pool=num("test_pool"),
        typecheck_pool=num("typecheck_pool"),
        signal=str(payload.get("signal") or ""),
        reason=str(payload.get("reason") or ""),
        gates_priority=bool(payload.get("gates_priority")),
        degraded=bool(payload.get("degraded")),
    )


def _cpu_avg60(pressure: Any) -> float | None:  # noqa: ANN401
    if not isinstance(pressure, dict):
        return None
    cpu = pressure.get("cpu")
    if not isinstance(cpu, dict):
        return None
    value = cpu.get("some_avg60")
    return float(value) if isinstance(value, int | float) else None


def _supervisor_state(supervisor: Supervisor) -> dict[str, Any]:
    rows = supervisor.status_rows()
    up = [r for r in rows if r["state"] == "running"]
    return {
        "components": rows,
        "up": len(up),
        "total": len(rows),
        "enabled": sum(1 for r in rows if r["enabled"]),
        "crash_looping": [r["component"] for r in rows if r["state"] == "crash_looping"],
        "restarts": sum(int(r["restarts"]) for r in rows),
    }


def _wrap(text: str, width: int) -> list[str]:
    """Greedy wrap. Long reasons are the norm here, not the exception."""
    if not text:
        return [""]
    words = text.split()
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        if len(current) + 1 + len(word) <= width:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def render_status(snapshot: dict[str, Any]) -> str:
    """The one-screen text render."""
    out: list[str] = []
    operator = snapshot.get("operator", "?")
    sup = snapshot.get("supervisor", {})
    out.append(f"fleet serve — operator {operator}")
    out.append("=" * 72)

    # --- components
    out.append("")
    out.append("COMPONENTS")
    for row in sup.get("components", []):
        mark = " " if row.get("enabled") else "-"
        state = str(row.get("state", "?")).upper()
        pid = row.get("pid")
        detail = f"pid {pid}" if pid else "no pid"
        if row.get("adopted"):
            detail += " (adopted)"
        out.append(
            f"  {mark} {row.get('component', '?'):<10} {state:<13} "
            f"restarts {row.get('restarts', 0):<3} {detail}"
        )
        if row.get("message"):
            for line in _wrap(str(row["message"]), 58):
                out.append(f"      {line}")
    looping = sup.get("crash_looping") or []
    if looping:
        out.append(f"  ! crash-looping, not restarted: {', '.join(looping)}")

    # --- capacity
    out.append("")
    cap = snapshot.get("capacity", {})
    out.append("CAPACITY")
    targets = cap.get("targets")
    if not cap.get("available") or targets is None:
        out.append("  no capacity file yet — serve has not published targets")
    else:
        state = "DEGRADED" if targets.get("degraded") else str(targets.get("signal", "?")).upper()
        out.append(
            f"  signal {state:<10} lanes {targets.get('max_lanes')} "
            f"gates {targets.get('max_gates')} "
            f"tests {targets.get('test_pool')} typecheck {targets.get('typecheck_pool')}"
        )
        if cap.get("gates_priority"):
            out.append("  gates_priority ON — holding lanes at floor to finish gates")
        if cap.get("pressure_ok"):
            avg60 = cap.get("cpu_some_avg60")
            mem = cap.get("memory_ratio")
            mem_text = f"{mem:.0%}" if isinstance(mem, int | float) else "unknown"
            out.append(
                f"  pressure cpu some-avg60 "
                f"{avg60 if avg60 is not None else '?'}  memory {mem_text}"
            )
            if cap.get("pressure_path"):
                out.append(f"  cgroup {cap['pressure_path']}")
        else:
            # The important line. Never render a failed read as a healthy zero.
            out.append("  pressure UNAVAILABLE — targets held at floor, not assumed idle:")
            for line in _wrap(str(cap.get("pressure_error") or "unknown reason"), 60):
                out.append(f"    {line}")
        if targets.get("reason") and not targets.get("degraded"):
            for line in _wrap(str(targets["reason"]), 60):
                out.append(f"  {line}")

    # --- stages
    out.append("")
    out.append("STAGES")
    header = (
        f"  {'stage':<{_STAGE_WIDTH}} {'depth':>5} {'/hr':>6}  "
        f"{'oldest':<10} {'age':>5}  why it waits"
    )
    out.append(header)
    out.append("  " + "-" * (len(header) - 2))
    for stat in snapshot.get("stages", []):
        oldest = stat.get("oldest_item") or "-"
        out.append(
            f"  {stat.get('stage', ''):<{_STAGE_WIDTH}} {stat.get('depth', 0):>5} "
            f"{stat.get('per_hour', 0.0):>6.1f}  {oldest:<10} "
            f"{_fmt_age(float(stat.get('oldest_age_s') or 0.0)):>5}"
            + (f"  {stat['wait_reason']}" if stat.get("wait_reason") else "")
        )

    out.append("")
    out.append(
        f"queue depth {snapshot.get('queue_depth', 0)}  ·  "
        f"{sup.get('up', 0)}/{sup.get('enabled', 0)} components up  ·  "
        f"{sup.get('restarts', 0)} restarts"
    )
    return "\n".join(out)


def degraded_note(snapshot: dict[str, Any]) -> str:
    """A one-line summary of anything that needs a human. Empty when all good."""
    notes: list[str] = []
    sup = snapshot.get("supervisor", {})
    if sup.get("crash_looping"):
        notes.append(f"crash-looping: {', '.join(sup['crash_looping'])}")
    cap = snapshot.get("capacity", {})
    if cap.get("available") and not cap.get("pressure_ok"):
        notes.append("pressure unavailable (targets at floor)")
    return "; ".join(notes)


__all__ = [
    "SIGNAL_UNKNOWN",
    "degraded_note",
    "render_status",
    "status_snapshot",
]
