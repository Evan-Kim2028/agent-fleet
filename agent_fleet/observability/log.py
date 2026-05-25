"""Structured run logging for fleet dispatches."""

# ruff: noqa: TC003

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_fleet.config import default_runs_dir
from agent_fleet.observability.context import bind_phase, get_run_context
from agent_fleet.observability.events import FleetEvent, RunContext
from agent_fleet.observability.sinks import (
    JsonlFileSink,
    LogSink,
    MemoryRingSink,
    PythonLoggingSink,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_DEFAULT_RUNS_DIR = default_runs_dir()


class RunLog:
    """Emit structured fleet events to one or more sinks."""

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
        include_python_logging: bool = True,
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
        ]
        if include_python_logging:
            sinks.append(PythonLoggingSink())
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
