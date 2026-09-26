"""Serve's event path.

Two destinations, one call. Every serve event goes to

1. the shared fleet event stream — ``$AGENT_FLEET_RUNS_DIR/<run_id>.jsonl`` as
   a real :class:`~agent_fleet.observability.events.FleetEvent`, so ``fleet``
   tooling, the run index and anything already reading events keep working; and
2. the serve-local mirror — ``serve/<operator>/events.jsonl`` — because a
   supervisor that is down cannot write to a stream whose directory another
   process may be rotating, and because ``serve status`` has to answer "what
   happened to this item" without walking every run log.

Event names are dotted and namespaced under ``serve.`` so they cannot collide
with another subsystem's names, which the event schema guard requires.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from agent_fleet.fleet_paths import default_runs_dir
from agent_fleet.observability.events import FleetEvent
from agent_fleet.serve.paths import events_path

if TYPE_CHECKING:
    from pathlib import Path

LEVEL_ERROR = "error"
LEVEL_WARN = "warning"
LEVEL_INFO = "info"


def serve_run_id(operator: str) -> str:
    """The run id every event from this operator's supervisor shares."""
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in operator) or "default"
    return f"serve-{safe}"


def emit_serve_event(
    operator: str,
    event: str,
    *,
    level: str = LEVEL_INFO,
    data: dict[str, Any] | None = None,
    run_id: str | None = None,
    to_fleet_stream: bool = True,
) -> FleetEvent:
    """Emit one serve event to both streams. Returns the event for the caller.

    A failure writing the shared stream is swallowed and the mirror still gets
    the event: losing the operator's visibility because a runs directory is
    temporarily unwritable is a worse outcome than a duplicated or delayed
    event, and the watchdog's remediations must not be blocked by a log write.
    """
    record = FleetEvent.now(
        run_id=run_id or serve_run_id(operator),
        event=event if "." in event else f"serve.{event}",
        level=level,
        data=dict(data or {}),
    )
    payload = record.to_json()

    if to_fleet_stream:
        try:
            path = default_runs_dir() / f"{record.run_id}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(payload + "\n")
        except OSError:
            pass

    try:
        mirror = events_path(operator)
        mirror.parent.mkdir(parents=True, exist_ok=True)
        with mirror.open("a", encoding="utf-8") as handle:
            handle.write(payload + "\n")
    except OSError:
        pass
    return record


def read_serve_events(operator: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    """Read the serve-local mirror, oldest first, optionally last *limit*."""
    path: Path = events_path(operator)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows[-limit:] if limit else rows


def alert(operator: str, event: str, reason: str, **data: Any) -> FleetEvent:  # noqa: ANN401
    """An event the operator must actually see: error level, always mirrored."""
    return emit_serve_event(
        operator,
        event,
        level=LEVEL_ERROR,
        data={"reason": reason, **data},
    )


def ensure_operator_dirs(operator: str) -> Path:
    """Create the serve layout; used by every entry point before touching state."""
    from agent_fleet.serve.paths import ensure_serve_dir

    return ensure_serve_dir(operator)


__all__ = [
    "LEVEL_ERROR",
    "LEVEL_INFO",
    "LEVEL_WARN",
    "alert",
    "emit_serve_event",
    "ensure_operator_dirs",
    "read_serve_events",
    "serve_run_id",
]
