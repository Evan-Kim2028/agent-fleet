"""Write cookbook-compatible `.canvas.tsx` files for Cursor IDE hot-reload."""

# ruff: noqa: TC003

from __future__ import annotations

import json
import logging
import threading
from importlib.resources import files
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def render_canvas_source(state: dict[str, Any]) -> str:
    """Render a self-contained `.canvas.tsx` from RunState JSON."""
    package = files("agent_fleet.orchestration.dag.data")
    prefix = (package / "canvas_prefix.tsx").read_text(encoding="utf-8")
    suffix = (package / "canvas_suffix.tsx").read_text(encoding="utf-8")
    state_literal = json.dumps(state, indent=2)
    return f"{prefix}{state_literal}{suffix}"


class DagCanvasWriter:
    """Debounced writer — latest state wins within the debounce window."""

    def __init__(self, canvas_path: Path, *, debounce_ms: int = 200) -> None:
        self._canvas_path = canvas_path
        self._debounce_s = max(debounce_ms, 0) / 1000.0
        self._pending: dict[str, Any] | None = None
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    @property
    def canvas_path(self) -> Path:
        return self._canvas_path

    def schedule(self, state: dict[str, Any]) -> None:
        with self._lock:
            self._pending = state
            if self._debounce_s <= 0:
                self._write_pending_unlocked()
                return
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce_s, self._flush_timer)
            self._timer.daemon = True
            self._timer.start()

    def flush(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._write_pending_unlocked()

    def _flush_timer(self) -> None:
        with self._lock:
            self._timer = None
            self._write_pending_unlocked()

    def _write_pending_unlocked(self) -> None:
        if self._pending is None:
            return
        snapshot = self._pending
        self._pending = None
        source = render_canvas_source(snapshot)
        self._canvas_path.parent.mkdir(parents=True, exist_ok=True)
        self._canvas_path.write_text(source, encoding="utf-8")
        logger.debug("Wrote canvas %s", self._canvas_path)
