"""Durable, typed, append-only run-event journal.

Two consumers:
  - Queryable observability surface (D4): fold_journal / query_by_run.
  - Idempotent crash-resume (D3, built later): RunState.pending_task_indices.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


class RunEventKind(StrEnum):
    run_started = "run_started"
    run_completed = "run_completed"
    agent_started = "agent_started"
    agent_completed = "agent_completed"
    agent_failed = "agent_failed"
    phase = "phase"
    log = "log"


@dataclass(frozen=True)
class RunEvent:
    run_id: str
    seq: int
    kind: RunEventKind
    ts: float
    agent_id: str | None = None
    task_index: int | None = None
    payload: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "seq": self.seq,
            "kind": str(self.kind),
            "ts": self.ts,
            "agent_id": self.agent_id,
            "task_index": self.task_index,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> RunEvent:
        # from_dict is the boundary: parse external (on-disk) data into typed objects.
        raw_ti = d.get("task_index")
        raw_ai = d.get("agent_id")
        raw_payload = d.get("payload")
        return cls(
            run_id=str(d["run_id"]),
            seq=int(str(d["seq"])),
            kind=RunEventKind(str(d["kind"])),
            ts=float(str(d["ts"])),
            agent_id=str(raw_ai) if raw_ai is not None else None,
            task_index=int(str(raw_ti)) if raw_ti is not None else None,
            payload=(
                dict(cast("dict[str, object]", raw_payload))
                if isinstance(raw_payload, dict) else {}
            ),
        )


# Terminal statuses that mark an agent as done and ok.
_TERMINAL_OK = frozenset({"completed", "merged", "review_changes_requested"})


@dataclass(frozen=True)
class AgentRecord:
    task_index: int
    agent_id: str | None
    persona: str
    goal: str
    status: str
    summary: str | None
    observed_total_tokens: int | None
    started: bool
    done: bool
    error: str | None
    # The output text the run propagated downstream. Richer than summary (it
    # folds in stdout); resume replays it as the reused task's upstream context.
    output: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in _TERMINAL_OK


@dataclass(frozen=True)
class RunState:
    run_id: str
    status: str
    agents: tuple[AgentRecord, ...]
    phases: tuple[str, ...]
    log: tuple[str, ...]
    started_at: float | None
    completed_at: float | None

    @property
    def completed_task_indices(self) -> frozenset[int]:
        """Agents that reached a terminal ok state. Resume keys on this."""
        return frozenset(a.task_index for a in self.agents if a.ok)

    def pending_task_indices(self, expected: Iterable[int]) -> frozenset[int]:
        """expected minus completed -- drives idempotent re-dispatch."""
        return frozenset(expected) - self.completed_task_indices

    def agent_by_index(self, i: int) -> AgentRecord | None:
        for a in self.agents:
            if a.task_index == i:
                return a
        return None


# Mutable intermediate state used during fold; never exposed externally.
@dataclass
class _AgentMutable:
    task_index: int
    agent_id: str | None = None
    persona: str = ""
    goal: str = ""
    status: str = "unknown"
    summary: str | None = None
    observed_total_tokens: int | None = None
    started: bool = False
    done: bool = False
    error: str | None = None
    output: str | None = None
    # seq of the last terminal-state write; used to deduplicate replayed events.
    _terminal_seq: int = -1

    def to_record(self) -> AgentRecord:
        return AgentRecord(
            task_index=self.task_index,
            agent_id=self.agent_id,
            persona=self.persona,
            goal=self.goal,
            status=self.status,
            summary=self.summary,
            observed_total_tokens=self.observed_total_tokens,
            started=self.started,
            done=self.done,
            error=self.error,
            output=self.output,
        )


def fold_journal(events: Iterable[RunEvent]) -> RunState:
    """Pure reduce over events to a RunState.

    Idempotent: folding the same multiset of events (even with duplicates)
    yields the same RunState. Terminal agent state is deduplicated by
    task_index; last-writer-wins by seq, so a completed event is never undone
    by a stale duplicate with a lower seq.
    """
    run_id: str = ""
    run_status: str = "unknown"
    started_at: float | None = None
    completed_at: float | None = None
    agents: dict[int, _AgentMutable] = {}
    phases: list[str] = []
    log_lines: list[str] = []

    # Sort by seq so last-writer-wins is well-defined even if events arrive
    # out of order.
    sorted_events = sorted(events, key=lambda e: e.seq)

    for evt in sorted_events:
        if not run_id:
            run_id = evt.run_id

        p = evt.payload
        kind = evt.kind
        ti = evt.task_index

        if kind == RunEventKind.run_started:
            run_status = "started"
            started_at = evt.ts

        elif kind == RunEventKind.run_completed:
            raw_status = p.get("status", "completed")
            run_status = str(raw_status) if raw_status is not None else "completed"
            completed_at = evt.ts

        elif kind == RunEventKind.agent_started:
            if ti is not None:
                if ti not in agents:
                    agents[ti] = _AgentMutable(task_index=ti)
                a = agents[ti]
                a.agent_id = evt.agent_id
                a.persona = str(p.get("persona", ""))
                a.goal = str(p.get("goal", ""))
                a.started = True

        elif kind == RunEventKind.agent_completed:
            if ti is not None:
                if ti not in agents:
                    agents[ti] = _AgentMutable(task_index=ti)
                a = agents[ti]
                # Last-writer-wins: only apply if this event's seq is newer than
                # the last recorded terminal-state seq for this agent.
                if evt.seq > a._terminal_seq:
                    a._terminal_seq = evt.seq
                    a.status = str(p.get("status", "completed"))
                    raw_summary = p.get("summary")
                    a.summary = str(raw_summary) if raw_summary is not None else None
                    raw_output = p.get("output")
                    a.output = str(raw_output) if raw_output is not None else None
                    raw_tokens = p.get("observed_total_tokens")
                    a.observed_total_tokens = (
                        int(str(raw_tokens)) if raw_tokens is not None else None
                    )
                    a.done = True
                    if evt.agent_id is not None:
                        a.agent_id = evt.agent_id

        elif kind == RunEventKind.agent_failed:
            if ti is not None:
                if ti not in agents:
                    agents[ti] = _AgentMutable(task_index=ti)
                a = agents[ti]
                if evt.seq > a._terminal_seq:
                    a._terminal_seq = evt.seq
                    a.status = str(p.get("status", "failed"))
                    raw_error = p.get("error")
                    a.error = str(raw_error) if raw_error is not None else None
                    a.done = True
                    if evt.agent_id is not None:
                        a.agent_id = evt.agent_id

        elif kind == RunEventKind.phase:
            title = str(p.get("title", ""))
            if title and title not in phases:
                phases.append(title)

        elif kind == RunEventKind.log:
            msg = str(p.get("message", ""))
            if msg:
                log_lines.append(msg)

    return RunState(
        run_id=run_id,
        status=run_status,
        agents=tuple(agents[i].to_record() for i in sorted(agents)),
        phases=tuple(phases),
        log=tuple(log_lines),
        started_at=started_at,
        completed_at=completed_at,
    )


# Module-level alias required by the harness.
fold = fold_journal


def load_journal(path: str | Path) -> list[RunEvent]:
    """Read a JSONL file back into typed RunEvent objects, tolerating blank lines."""
    events: list[RunEvent] = []
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            events.append(RunEvent.from_dict(json.loads(stripped)))
    return events


def query_by_run(source: str | Path | list[RunEvent], run_id: str) -> RunState:
    """Load if needed, filter to run_id, and return fold_journal of that slice."""
    events: list[RunEvent] = source if isinstance(source, list) else load_journal(source)
    filtered = [e for e in events if e.run_id == run_id]
    return fold_journal(filtered)


class RunJournal:
    """Append-only writer/reader over a JSONL file.

    Thread-safe: a single lock guards both the seq counter and the write, so
    parallel agents appending concurrently get unique monotonic seq values and
    no interleaved writes.
    """

    def __init__(self, path: str | Path, run_id: str, *, start_seq: int = 0) -> None:
        self._path = Path(path)
        self.run_id = run_id
        # start_seq lets a resumed run continue the seq counter past the events
        # already on disk, so seq stays globally unique within a run across a
        # restart and last-writer-wins stays well-defined.
        self._seq = start_seq
        self._lock = threading.Lock()
        self._events: list[RunEvent] = []
        self._fh = self._path.open("a", encoding="utf-8")

    def append(
        self,
        kind: RunEventKind,
        *,
        agent_id: str | None = None,
        task_index: int | None = None,
        **payload: object,
    ) -> RunEvent:
        with self._lock:
            seq = self._seq
            self._seq += 1
            evt = RunEvent(
                run_id=self.run_id,
                seq=seq,
                kind=kind,
                ts=time.time(),
                agent_id=agent_id,
                task_index=task_index,
                payload=dict(payload),
            )
            line = json.dumps(evt.to_dict()) + "\n"
            self._fh.write(line)
            self._fh.flush()
            # fsync before returning so a crash after append cannot lose the event.
            os.fsync(self._fh.fileno())
            self._events.append(evt)
        return evt

    # Convenience helpers -- all delegate to .append.

    def run_started(self) -> RunEvent:
        return self.append(RunEventKind.run_started)

    def run_completed(self, *, status: str = "completed") -> RunEvent:
        return self.append(RunEventKind.run_completed, status=status)

    def agent_started(
        self,
        task_index: int,
        *,
        agent_id: str | None = None,
        persona: str = "",
        goal: str = "",
    ) -> RunEvent:
        return self.append(
            RunEventKind.agent_started,
            agent_id=agent_id,
            task_index=task_index,
            persona=persona,
            goal=goal,
        )

    def agent_completed(
        self,
        task_index: int,
        *,
        agent_id: str | None = None,
        status: str = "completed",
        summary: str | None = None,
        observed_total_tokens: int | None = None,
        output: str | None = None,
    ) -> RunEvent:
        return self.append(
            RunEventKind.agent_completed,
            agent_id=agent_id,
            task_index=task_index,
            status=status,
            summary=summary,
            observed_total_tokens=observed_total_tokens,
            output=output,
        )

    def agent_failed(
        self,
        task_index: int,
        *,
        agent_id: str | None = None,
        error: str | None = None,
    ) -> RunEvent:
        return self.append(
            RunEventKind.agent_failed,
            agent_id=agent_id,
            task_index=task_index,
            status="failed",
            error=error,
        )

    def log_phase(self, title: str) -> RunEvent:
        return self.append(RunEventKind.phase, title=title)

    def log_message(self, message: str) -> RunEvent:
        return self.append(RunEventKind.log, message=message)

    def events(self) -> list[RunEvent]:
        with self._lock:
            return list(self._events)

    def state(self) -> RunState:
        return fold_journal(self.events())

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()

    def __enter__(self) -> RunJournal:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
