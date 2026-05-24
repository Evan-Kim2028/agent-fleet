"""Pluggable sinks for structured fleet events."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_fleet.observability.events import FleetEvent

logger = logging.getLogger(__name__)


class LogSink(ABC):
    @abstractmethod
    def emit(self, event: FleetEvent) -> None:
        raise NotImplementedError


class JsonlFileSink(LogSink):
    """Append one JSON object per line to a run log file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def emit(self, event: FleetEvent) -> None:
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(event.to_json())
            handle.write("\n")


class PythonLoggingSink(LogSink):
    """Mirror fleet events into the standard Python logging tree."""

    def __init__(self, logger_name: str = "agent_fleet.events") -> None:
        self._logger = logging.getLogger(logger_name)

    def emit(self, event: FleetEvent) -> None:
        level = {
            "debug": logging.DEBUG,
            "info": logging.INFO,
            "warning": logging.WARNING,
            "error": logging.ERROR,
        }.get(event.level, logging.INFO)
        summary = event.event
        if event.phase:
            summary = f"{event.phase} {summary}"
        if event.data:
            summary = f"{summary} {event.data}"
        self._logger.log(level, "[%s] %s", event.run_id, summary)


class MemoryRingSink(LogSink):
    """Keep the most recent events in memory for quick status queries."""

    def __init__(self, *, max_events: int = 500) -> None:
        self._events: deque[FleetEvent] = deque(maxlen=max_events)

    @property
    def events(self) -> tuple[FleetEvent, ...]:
        return tuple(self._events)

    def emit(self, event: FleetEvent) -> None:
        self._events.append(event)
