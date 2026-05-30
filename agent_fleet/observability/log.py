"""Structured run logging for fleet dispatches."""

# ruff: noqa: TC003

from __future__ import annotations

import contextlib
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_fleet.config import default_runs_dir
from agent_fleet.observability.context import bind_phase, get_run_context
from agent_fleet.observability.events import FleetEvent, RunContext
from agent_fleet.observability.logfire_sink import LogfireSink
from agent_fleet.observability.run_store import append_run_index_row
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
        self._usage_rollup_emitted: bool = False
        self._last_usage_rollup: dict[str, Any] | None = None

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
        import time

        t0 = time.monotonic()
        start_payload: dict[str, Any] = {"phase": name, "run_id": self.run_id}
        if data:
            start_payload.update(data)
        with bind_phase(name):
            self.emit("phase.start", phase=name, data=start_payload)
            status = "completed"
            try:
                yield name
            except Exception:
                status = "failed"
                raise
            finally:
                wall_s = round(time.monotonic() - t0, 3)
                end_payload: dict[str, Any] = {
                    "phase": name,
                    "run_id": self.run_id,
                    "wall_s": wall_s,
                    "status": status,
                }
                self.emit("phase.end", phase=name, data=end_payload)

    def _index_runs_dir(self) -> Path | None:
        path = self.jsonl_path
        return path.parent if path is not None else None

    def run_start(self, **data: object) -> None:
        self.emit("run.start", data=dict(data))
        append_run_index_row(
            {
                "run_id": self.run_id,
                "goal": str(data.get("title", "")),
                "status": "running",
                "started_at": time.time(),
                "persona": self.context.persona if self.context else None,
                "issue_number": self.context.issue_number if self.context else None,
            },
            runs_dir=self._index_runs_dir(),
        )

    def run_end(self, *, outcome: str, changed_lines: int = 0, **data: object) -> None:
        # Flush per-task token rollup before the run terminates so the
        # consolidated log carries totals even for ad-hoc `agent-fleet run`
        # paths that don't pass through the dispatcher.
        if self._usage_calls > 0:
            self.task_usage_rollup(
                task_id=self.context.task_id if self.context else None,
                changed_lines=changed_lines,
            )
        payload = {"outcome": outcome, **data}
        self.emit("run.end", data=payload)
        rollup = self._last_usage_rollup
        total_tokens = rollup["totals"]["total_tokens"] if rollup else 0
        # Merge-update the index row: run_start wrote goal/started_at, so this
        # later row only needs to carry the fields that changed.
        append_run_index_row(
            {
                "run_id": self.run_id,
                "status": outcome,
                "tokens": total_tokens,
                "completed_at": time.time(),
            },
            runs_dir=self._index_runs_dir(),
        )
        # Prefer the caller-supplied changed_lines when nonzero: the idempotency
        # guard in task_usage_rollup may have fired before changed_lines was known
        # (dispatcher path calls _peek_usage_rollup with changed_lines=0 first).
        _cl = (
            changed_lines
            if changed_lines > 0
            else (rollup.get("changed_lines", 0) if rollup else 0)
        )
        tpl = round(total_tokens / max(_cl, 1))
        by_phase = rollup.get("by_phase", {}) if rollup else {}
        by_phase_str = ",".join(f"{ph}:{v['total_tokens']}" for ph, v in sorted(by_phase.items()))
        self.emit(
            "efficiency.headline",
            data={
                "run_id": self.run_id,
                "total_tokens": total_tokens,
                "changed_lines": _cl,
                "tokens_per_changed_line": tpl,
                "by_phase": by_phase_str,
                "headline": (
                    f"EFFICIENCY run={self.run_id} total_tokens={total_tokens}"
                    f" changed_lines={_cl} tokens_per_changed_line={tpl}"
                    f" by_phase={by_phase_str}"
                ),
            },
        )

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
        tokens: dict[str, int] = {
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "cache_read_tokens": int(cache_read_tokens),
            "cache_write_tokens": int(cache_write_tokens),
        }
        tokens["total_tokens"] = sum(tokens.values())
        data: dict[str, Any] = {
            "model": model,
            "agent_id": agent_id,
            "duration_s": round(duration_s, 3),
            **tokens,
        }
        self._usage_calls += 1
        self._usage_duration_s += duration_s
        for key in self._USAGE_FIELDS:
            self._usage_totals[key] += tokens[key]
        _ctx = get_run_context() or self.context
        phase_key = phase or (_ctx.phase if _ctx else None) or "unknown"
        bucket = self._usage_by_phase.setdefault(
            phase_key, dict.fromkeys(self._USAGE_FIELDS, 0) | {"calls": 0}
        )
        for key in self._USAGE_FIELDS:
            bucket[key] += tokens[key]
        bucket["calls"] += 1
        self.emit("llm.usage", phase=phase, data=data)

    def _usage_rollup_payload(
        self, *, task_id: int | None = None, changed_lines: int = 0
    ) -> dict[str, Any] | None:
        if self._usage_calls == 0:
            return None
        totals = dict(self._usage_totals)
        totals["total_tokens"] = sum(totals.values())
        by_phase: dict[str, dict[str, int]] = {}
        for phase_key, bucket in self._usage_by_phase.items():
            entry = dict(bucket)
            entry["total_tokens"] = sum(entry[k] for k in self._USAGE_FIELDS)
            by_phase[phase_key] = entry
        total_tokens = totals["total_tokens"]
        return {
            "task_id": task_id,
            "calls": self._usage_calls,
            "duration_s": round(self._usage_duration_s, 3),
            "totals": totals,
            "by_phase": by_phase,
            "changed_lines": changed_lines,
            "tokens_per_changed_line": round(total_tokens / max(changed_lines, 1)),
        }

    def usage_rollup_snapshot(self, *, task_id: int | None = None) -> dict[str, Any] | None:
        """Return per-task token rollup without emitting (safe to call multiple times)."""
        if self._last_usage_rollup is not None:
            return dict(self._last_usage_rollup)
        return self._usage_rollup_payload(task_id=task_id)

    def task_usage_rollup(
        self, *, task_id: int | None = None, changed_lines: int = 0
    ) -> dict[str, Any] | None:
        """Emit a per-task usage summary; returns the totals payload (or None).
        Idempotent: only emits once per RunLog lifetime."""
        if self._usage_calls == 0:
            return None
        if self._usage_rollup_emitted:
            return self._last_usage_rollup
        payload = self._usage_rollup_payload(task_id=task_id, changed_lines=changed_lines)
        if payload is None:
            return None
        self._usage_rollup_emitted = True
        self._last_usage_rollup = payload
        self.emit("llm.usage.task_rollup", data=payload)
        return payload
