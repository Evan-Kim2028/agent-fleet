"""Structured run logging for fleet dispatches."""

# ruff: noqa: TC003

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_fleet.config import default_runs_dir
from agent_fleet.observability.context import bind_phase, get_run_context
from agent_fleet.observability.events import FleetEvent, RunContext
from agent_fleet.observability.logfire_sink import LogfireSink
from agent_fleet.observability.sinks import (
    JsonlFileSink,
    LogSink,
    MemoryRingSink,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_DEFAULT_RUNS_DIR = default_runs_dir()


class RunLog:
    """Emit structured fleet events to one or more sinks."""

    _USAGE_FIELDS = (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    )

    def __init__(
        self,
        *,
        run_id: str,
        context: RunContext,
        sinks: list[LogSink],
    ) -> None:
        self.run_id = run_id
        self.context = context
        self._sinks = sinks
        # Accumulates token usage across every llm_usage() emit so the
        # dispatcher can flush a single per-task rollup before
        # fleet.task.complete. Per-phase subtotals plus the grand total
        # live side-by-side; missing fields default to zero.
        self._usage_totals: dict[str, int] = dict.fromkeys(self._USAGE_FIELDS, 0)
        self._usage_calls: int = 0
        self._usage_duration_s: float = 0.0
        self._usage_by_phase: dict[str, dict[str, int]] = {}

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        issue_number: int | None = None,
        task_id: int | None = None,
        persona: str | None = None,
        visual_audit: bool = False,
        runs_dir: Path | None = None,
        include_memory_ring: bool = True,
    ) -> RunLog:
        context = RunContext(
            run_id=run_id,
            issue_number=issue_number,
            task_id=task_id,
            persona=persona,
            visual_audit=visual_audit,
        )
        sinks: list[LogSink] = [
            JsonlFileSink((runs_dir or _DEFAULT_RUNS_DIR) / f"{run_id}.jsonl"),
            LogfireSink(),
        ]
        if include_memory_ring:
            sinks.append(MemoryRingSink())
        return cls(run_id=run_id, context=context, sinks=sinks)

    @property
    def jsonl_path(self) -> Path | None:
        for sink in self._sinks:
            if isinstance(sink, JsonlFileSink):
                return sink.path
        return None

    def emit(
        self,
        event: str,
        *,
        level: str = "info",
        phase: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> FleetEvent:
        ctx = get_run_context() or self.context
        fleet_event = FleetEvent.now(
            run_id=self.run_id,
            event=event,
            level=level,
            phase=phase or ctx.phase,
            issue_number=ctx.issue_number,
            persona=ctx.persona,
            data=data,
        )
        for sink in self._sinks:
            sink.emit(fleet_event)
        return fleet_event

    @contextlib.contextmanager
    def phase(self, name: str, **data: object) -> Iterator[str]:
        with bind_phase(name):
            self.emit("phase.start", phase=name, data=data or None)
            try:
                yield name
            finally:
                self.emit("phase.end", phase=name)

    def run_start(self, **data: object) -> None:
        self.emit("run.start", data=dict(data))

    def run_end(self, *, outcome: str, **data: object) -> None:
        payload = {"outcome": outcome, **data}
        self.emit("run.end", data=payload)

    def memory(self, **snapshot: object) -> None:
        self.emit("memory.snapshot", data=dict(snapshot))

    def admission(self, *, allowed: bool, reason: str, **data: object) -> None:
        self.emit(
            "admission.check",
            level="info" if allowed else "warning",
            data={"allowed": allowed, "reason": reason, **data},
        )

    def mcp_tool(self, *, action: str, tool: str, **data: object) -> None:
        self.emit(f"mcp.tool.{action}", data={"tool": tool, **data})

    def mcp_requirement(self, *, passed: bool, **data: object) -> None:
        self.emit(
            "mcp.requirement.check",
            level="info" if passed else "error",
            data={"passed": passed, **data},
        )

    def llm_usage(
        self,
        *,
        phase: str | None,
        model: str | None,
        duration_s: float,
        agent_id: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None:
        """Record one LLM call's token usage and accumulate per-task totals."""
        data = {
            "model": model,
            "agent_id": agent_id,
            "duration_s": round(duration_s, 3),
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "cache_read_tokens": int(cache_read_tokens),
            "cache_write_tokens": int(cache_write_tokens),
        }
        self._usage_calls += 1
        self._usage_duration_s += duration_s
        for key in self._USAGE_FIELDS:
            self._usage_totals[key] += int(data[key])
        phase_key = phase or (self.context.phase if self.context else None) or "unknown"
        bucket = self._usage_by_phase.setdefault(
            phase_key, dict.fromkeys(self._USAGE_FIELDS, 0) | {"calls": 0}
        )
        for key in self._USAGE_FIELDS:
            bucket[key] += int(data[key])
        bucket["calls"] += 1
        self.emit("llm.usage", phase=phase, data=data)

    def task_usage_rollup(self, *, task_id: int | None = None) -> dict[str, Any] | None:
        """Emit a per-task usage summary; returns the totals payload (or None)."""
        if self._usage_calls == 0:
            return None
        payload: dict[str, Any] = {
            "task_id": task_id,
            "calls": self._usage_calls,
            "duration_s": round(self._usage_duration_s, 3),
            "totals": dict(self._usage_totals),
            "by_phase": {p: dict(b) for p, b in self._usage_by_phase.items()},
        }
        self.emit("llm.usage.task_rollup", data=payload)
        return payload
