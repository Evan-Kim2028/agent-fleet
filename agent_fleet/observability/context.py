"""Context-local run log and run context."""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from typing import TYPE_CHECKING

from agent_fleet.observability.events import RunContext

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agent_fleet.observability.log import RunLog

_run_log: ContextVar[RunLog | None] = ContextVar("fleet_run_log", default=None)
_run_context: ContextVar[RunContext | None] = ContextVar("fleet_run_context", default=None)


def get_run_log() -> RunLog | None:
    return _run_log.get()


def get_run_context() -> RunContext | None:
    return _run_context.get()


@contextlib.contextmanager
def bind_run(
    run_log: RunLog,
    context: RunContext,
) -> Iterator[RunLog]:
    """Bind *run_log* and *context* for the current async/task context."""
    log_token = _run_log.set(run_log)
    ctx_token = _run_context.set(context)
    try:
        yield run_log
    finally:
        _run_log.reset(log_token)
        _run_context.reset(ctx_token)


@contextlib.contextmanager
def bind_phase(phase: str) -> Iterator[str]:
    """Temporarily set the active phase on the current run context."""
    ctx = _run_context.get()
    if ctx is None:
        yield phase
        return
    updated = RunContext(
        run_id=ctx.run_id,
        issue_number=ctx.issue_number,
        task_id=ctx.task_id,
        persona=ctx.persona,
        visual_audit=ctx.visual_audit,
        phase=phase,
    )
    token = _run_context.set(updated)
    try:
        yield phase
    finally:
        _run_context.reset(token)
