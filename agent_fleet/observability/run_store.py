"""Human-facing run store: a canonical index plus a fold of the rich stream.

This unifies the two run-event systems at the read layer. Every run already
writes a rich ``FleetEvent`` stream to the canonical runs dir; the durable
``RunJournal`` writes a typed ``RunEvent`` stream that folds to ``RunState``.
Rather than fold two shapes two ways, this module maps the rich stream onto the
same ``RunEvent`` vocabulary and reuses the existing, tested ``fold_journal`` ->
``RunState``. The result: one ``RunState`` shape and one renderer drive both
``fleet runs`` (over an append-only index) and ``fleet watch`` (tailing a run's
rich stream).
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from agent_fleet.fleet_paths import default_runs_dir
from agent_fleet.orchestration.journal import (
    RunEvent,
    RunEventKind,
    RunState,
    fold_journal,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

_INDEX_NAME = "index.jsonl"
_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "error", "rejected", "cancelled"})


# ---------------------------------------------------------------------------
# Append-only run index
# ---------------------------------------------------------------------------


def run_index_path(runs_dir: Path | None = None) -> Path:
    return (runs_dir or default_runs_dir()) / _INDEX_NAME


def append_run_index_row(row: Mapping[str, object], *, runs_dir: Path | None = None) -> None:
    """Append one run-summary row to the canonical index. Never raises.

    Logging must never crash a run, so any filesystem error is swallowed. Rows
    are merged by ``run_id`` on read, so a later row carrying only the fields
    that changed (status, tokens) updates an earlier start row in place.
    """
    path = run_index_path(runs_dir)
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(row), default=str, ensure_ascii=False))
            fh.write("\n")


def read_run_index(*, runs_dir: Path | None = None) -> list[dict[str, object]]:
    """Merge the index by run_id (last-writer-wins per field), newest started first."""
    path = run_index_path(runs_dir)
    if not path.exists():
        return []
    merged: dict[str, dict[str, object]] = {}
    order: list[str] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(row, dict):
                continue
            rid = str(row.get("run_id", ""))
            if not rid:
                continue
            if rid not in merged:
                merged[rid] = {}
                order.append(rid)
            merged[rid].update(row)
    rows = [merged[rid] for rid in order]
    rows.sort(key=lambda r: _coerce_float(r.get("started_at")) or 0.0, reverse=True)
    return rows


# ---------------------------------------------------------------------------
# Fold the rich FleetEvent stream into RunState (reusing fold_journal)
# ---------------------------------------------------------------------------


def load_fleet_events(path: str | Path) -> list[dict[str, object]]:
    """Read a ``<run_id>.jsonl`` rich stream into dicts, tolerating partial lines."""
    rows: list[dict[str, object]] = []
    p = Path(path)
    if not p.exists():
        return rows
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def fleet_events_to_run_events(rows: Iterable[Mapping[str, object]]) -> list[RunEvent]:
    """Map the rich FleetEvent stream onto durable RunEvents so fold_journal applies.

    ``seq`` is the stream position. The rich stream is append-only and written
    in chronological order, so position is a valid monotonic key and fold's
    last-writer-wins stays well-defined without a stored counter.
    """
    out: list[RunEvent] = []
    for seq, row in enumerate(rows):
        mapped = _map_event(seq, row)
        if mapped is not None:
            out.append(mapped)
    return out


def fold_run_events(rows: Iterable[Mapping[str, object]]) -> RunState:
    """Fold already-loaded rich rows into RunState (one read for watch ticks)."""
    return fold_journal(fleet_events_to_run_events(rows))


def fold_run_log(path: str | Path) -> RunState:
    """Fold a run's rich event stream into the shared RunState shape."""
    return fold_journal(fleet_events_to_run_events(load_fleet_events(path)))


def run_log_total_tokens(rows: Iterable[Mapping[str, object]]) -> int:
    """Best-effort run-level token total from the rich stream.

    Prefers the run's efficiency headline or per-task usage rollup; falls back
    to summing per-agent tokens from the program stream.
    """
    headline = 0
    agent_sum = 0
    for row in rows:
        event = str(row.get("event", ""))
        data = _as_dict(row.get("data"))
        if event == "efficiency.headline":
            headline = _coerce_int(data.get("total_tokens")) or headline
        elif event == "llm.usage.task_rollup":
            totals = _as_dict(data.get("totals"))
            headline = _coerce_int(totals.get("total_tokens")) or headline
        elif event == "program.agent.done":
            agent_sum += _coerce_int(data.get("tokens")) or 0
    return headline or agent_sum


def _map_event(seq: int, row: Mapping[str, object]) -> RunEvent | None:
    event = str(row.get("event", ""))
    run_id = str(row.get("run_id", ""))
    data = _as_dict(row.get("data"))
    ts = _coerce_ts(row.get("ts"))

    if event == "run.start":
        return RunEvent(
            run_id=run_id, seq=seq, kind=RunEventKind.run_started, ts=ts,
            payload={"goal": data.get("title", "")},
        )
    if event == "run.end":
        return RunEvent(
            run_id=run_id, seq=seq, kind=RunEventKind.run_completed, ts=ts,
            payload={"status": str(data.get("outcome", "completed"))},
        )
    if event == "program.agent.start":
        return RunEvent(
            run_id=run_id, seq=seq, kind=RunEventKind.agent_started, ts=ts,
            task_index=_coerce_int(data.get("idx")),
            payload={"persona": data.get("persona", ""), "goal": data.get("goal", "")},
        )
    if event == "program.agent.done":
        return RunEvent(
            run_id=run_id, seq=seq, kind=RunEventKind.agent_completed, ts=ts,
            task_index=_coerce_int(data.get("idx")),
            payload={
                "status": str(data.get("status", "completed")),
                "observed_total_tokens": data.get("tokens"),
            },
        )
    if event == "program.phase":
        return RunEvent(
            run_id=run_id, seq=seq, kind=RunEventKind.phase, ts=ts,
            payload={"title": str(data.get("title", ""))},
        )
    if event == "phase.start":
        title = data.get("phase") or row.get("phase") or ""
        return RunEvent(
            run_id=run_id, seq=seq, kind=RunEventKind.phase, ts=ts,
            payload={"title": str(title)},
        )
    if event == "program.log":
        return RunEvent(
            run_id=run_id, seq=seq, kind=RunEventKind.log, ts=ts,
            payload={"message": str(data.get("message", ""))},
        )
    return None


# ---------------------------------------------------------------------------
# Resolve a run id (or prefix, or "latest") to its rich-stream file
# ---------------------------------------------------------------------------


def list_run_files(runs_dir: Path | None = None) -> list[Path]:
    directory = runs_dir or default_runs_dir()
    if not directory.exists():
        return []
    return [p for p in directory.glob("*.jsonl") if p.name != _INDEX_NAME]


def resolve_run_path(run_id: str, *, runs_dir: Path | None = None) -> Path | None:
    """Find a run's stream by exact id, then unique prefix, then 'latest'."""
    files = list_run_files(runs_dir)
    if not files:
        return None
    if run_id in ("latest", "last", ""):
        return max(files, key=lambda p: p.stat().st_mtime)
    exact = [p for p in files if p.stem == run_id]
    if exact:
        return exact[0]
    prefix = sorted(p for p in files if p.stem.startswith(run_id))
    if prefix:
        return prefix[0]
    return None


def run_is_terminal(state: RunState) -> bool:
    return state.status in _TERMINAL_RUN_STATUSES


# ---------------------------------------------------------------------------
# Pure renderers (RunState/rows -> text)
# ---------------------------------------------------------------------------


def render_runs_table(rows: list[dict[str, object]]) -> str:
    """Render the run index as a fixed-column table. Pure: input rows -> text."""
    if not rows:
        return "No runs recorded yet."
    header = f"{'RUN ID':<28}  {'STATUS':<10}  {'TOKENS':>9}  {'STARTED':<16}  GOAL"
    lines = [header, "-" * len(header)]
    for row in rows:
        run_id = str(row.get("run_id", ""))[:28]
        status = str(row.get("status", "?"))[:10]
        tokens = _coerce_int(row.get("tokens")) or 0
        started = _format_ts(_coerce_float(row.get("started_at")))
        goal = " ".join(str(row.get("goal", "")).split())[:48]
        lines.append(f"{run_id:<28}  {status:<10}  {tokens:>9}  {started:<16}  {goal}")
    return "\n".join(lines)


def render_run_state(state: RunState, *, tokens: int | None = None) -> str:
    """Render a folded RunState as a phase/agent tree. Pure: input state -> text."""
    head = f"run {state.run_id or '?'}  [{state.status}]"
    if tokens is not None:
        head += f"  tokens={tokens}"
    lines = [head]
    if state.phases:
        lines.append("phases: " + " -> ".join(state.phases))
    if state.agents:
        lines.append("agents:")
        for agent in state.agents:
            mark = "ok" if agent.ok else ("done" if agent.done else "run")
            tok = f"  {agent.observed_total_tokens}t" if agent.observed_total_tokens else ""
            goal = (" " + " ".join(agent.goal.split())[:48]) if agent.goal else ""
            persona = agent.persona or "?"
            lines.append(
                f"  #{agent.task_index} {persona:<10} [{agent.status}] {mark}{tok}{goal}"
            )
    if state.log:
        lines.append("log (last 5):")
        lines.extend(f"  - {line}" for line in state.log[-5:])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Coercion helpers (on-disk data is untrusted)
# ---------------------------------------------------------------------------


def _as_dict(value: object) -> dict[str, object]:
    """Narrow an untrusted on-disk value to a string-keyed dict (else empty)."""
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items()}
    return {}


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip():
        with contextlib.suppress(ValueError):
            return int(float(value))
    return None


def _coerce_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        with contextlib.suppress(ValueError):
            return float(value)
    return None


def _coerce_ts(value: object) -> float:
    epoch = _coerce_float(value)
    if epoch is not None:
        return epoch
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            return datetime.fromisoformat(value).timestamp()
    return 0.0


def _format_ts(epoch: float | None) -> str:
    if not epoch:
        return "-"
    with contextlib.suppress(ValueError, OSError, OverflowError):
        return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")
    return "-"
