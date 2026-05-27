"""Mirror FleetEvents into Logfire as span events / log records."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agent_fleet.observability.sinks import LogSink

if TYPE_CHECKING:
    from agent_fleet.observability.events import FleetEvent

logger = logging.getLogger(__name__)

_LEVEL_TO_LOGFIRE = {
    "debug": "debug",
    "info": "info",
    "warning": "warn",
    "error": "error",
}


class LogfireSink(LogSink):
    """Forward fleet events to Logfire (which writes locally per telemetry.py).

    Each FleetEvent becomes a Logfire log entry attached to the current span.
    Top-level dispatch + cursor.chat spans are created elsewhere; this sink
    keeps the fine-grained event stream attached to them for correlation.
    """

    def __init__(self) -> None:
        try:
            import logfire
        except ImportError:
            self._logfire = None
            return
        self._logfire = logfire

    def emit(self, event: FleetEvent) -> None:
        if self._logfire is None:
            return
        method_name = _LEVEL_TO_LOGFIRE.get(event.level, "info")
        log_fn = getattr(self._logfire, method_name, self._logfire.info)
        attrs: dict[str, object] = {
            "run_id": event.run_id,
            "event": event.event,
        }
        if event.phase:
            attrs["phase"] = event.phase
        if event.issue_number is not None:
            attrs["issue_number"] = event.issue_number
        if event.persona:
            attrs["persona"] = event.persona
        if event.data:
            for key, value in event.data.items():
                attrs[f"data.{key}"] = value
        try:
            log_fn(event.event, _tags=None, **attrs)  # type: ignore[arg-type]
        except Exception as exc:
            logger.debug("logfire sink emit failed: %s", exc)
