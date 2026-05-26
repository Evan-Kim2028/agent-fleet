"""Unified fleet logging: RunLog + optional progress callback bridge."""

from __future__ import annotations

import contextlib
import logging
import uuid
from typing import TYPE_CHECKING

from agent_fleet.observability.context import bind_run, get_run_log
from agent_fleet.observability.log import RunLog

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)

_WATCHER_LOGGER: FleetLogger | None = None


class FleetLogger:
    """Facade over RunLog that also bridges optional progress callbacks."""

    def __init__(
        self,
        run_log: RunLog,
        *,
        progress_callback: Callable[..., None] | None = None,
    ) -> None:
        self._run_log = run_log
        self._progress_callback = progress_callback

    @property
    def run_log(self) -> RunLog:
        return self._run_log

    @property
    def run_id(self) -> str:
        return self._run_log.run_id

    @classmethod
    def for_dispatch(
        cls,
        *,
        task_index: int,
        persona: str | None = None,
        issue_number: int | None = None,
        runs_dir: Path | None = None,
        progress_callback: Callable[..., None] | None = None,
        visual_audit: bool = False,
    ) -> FleetLogger:
        run_id = f"dispatch-{task_index}-{uuid.uuid4().hex[:8]}"
        run_log = RunLog.create(
            run_id=run_id,
            task_id=task_index,
            issue_number=issue_number,
            persona=persona,
            visual_audit=visual_audit,
            runs_dir=runs_dir,
        )
        return cls(run_log, progress_callback=progress_callback)

    @classmethod
    def for_background(
        cls,
        *,
        run_id: str,
        issue_number: int | None = None,
        persona: str | None = None,
        runs_dir: Path | None = None,
        visual_audit: bool = False,
    ) -> FleetLogger:
        """Structured logger for watchers and PR loop (no progress callback)."""
        run_log = RunLog.create(
            run_id=run_id,
            issue_number=issue_number,
            persona=persona,
            visual_audit=visual_audit,
            runs_dir=runs_dir,
            include_memory_ring=False,
        )
        return cls(run_log)

    def emit(self, event: str, *, level: str = "info", **payload: object) -> None:
        data = dict(payload) if payload else None
        self._run_log.emit(event, level=level, data=data)
        if self._progress_callback is not None:
            try:
                self._progress_callback(event, **payload)
            except Exception as exc:
                logger.debug("Fleet progress callback failed: %s", exc)

    @contextlib.contextmanager
    def bind(self) -> Iterator[FleetLogger]:
        with bind_run(self._run_log, self._run_log.context):
            yield self


def get_watcher_logger() -> FleetLogger:
    """Singleton logger for issue/PR watcher background events."""
    global _WATCHER_LOGGER
    if _WATCHER_LOGGER is None:
        _WATCHER_LOGGER = FleetLogger.for_background(run_id="watcher")
    return _WATCHER_LOGGER


def emit_fleet_event(
    event: str,
    *,
    level: str = "info",
    use_watcher_fallback: bool = True,
    **payload: object,
) -> None:
    """Emit to bound RunLog, else watcher JSONL when ``use_watcher_fallback``."""
    run_log = get_run_log()
    data = dict(payload) if payload else None
    if run_log is not None:
        run_log.emit(event, level=level, data=data)
    elif use_watcher_fallback:
        get_watcher_logger().emit(event, level=level, **payload)
    if payload:
        logger.debug("%s %s", event, payload)
    else:
        logger.debug("%s", event)
