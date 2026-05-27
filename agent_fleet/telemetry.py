"""Local-first OpenTelemetry wiring for fleet runs.

Logfire is used as the instrumentation API (spans, attributes, tool-call
auto-capture) but configured with ``send_to_logfire=False`` so nothing leaves
the host. Spans are written as JSONL under
``$AGENT_FLEET_TELEMETRY_DIR`` (default ``~/.agent-fleet/traces``) — one file
per UTC date. Each line is the span dict serialized with default OTel
attributes plus our run-context attributes; ingest into DuckDB with
``read_json_auto`` for SQL queries.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from opentelemetry.context import Context
    from opentelemetry.trace import Span

logger = logging.getLogger(__name__)

_CONFIGURED = False
_LOCK = threading.Lock()


def _default_telemetry_dir() -> Path:
    env = os.environ.get("AGENT_FLEET_TELEMETRY_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".agent-fleet" / "traces"


def _today_jsonl(base: Path) -> Path:
    return base / f"spans-{datetime.now(UTC).strftime('%Y-%m-%d')}.jsonl"


class _JsonlSpanProcessor(SpanProcessor):
    """``SpanProcessor`` that appends finished spans as JSONL."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:  # type: ignore[override]
        del span, parent_context
        return

    def on_end(self, span: ReadableSpan) -> None:
        try:
            payload = self._serialize(span)
        except Exception as exc:  # serialization must never break the host
            logger.debug("telemetry serialize failed: %s", exc)
            return
        path = _today_jsonl(self._base_dir)
        with self._write_lock, path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str, ensure_ascii=False))
            handle.write("\n")

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        del timeout_millis
        return True

    @staticmethod
    def _serialize(span: ReadableSpan) -> dict[str, object]:
        ctx = span.get_span_context()
        parent = span.parent
        attrs = dict(span.attributes or {})
        trace_id = format(ctx.trace_id, "032x") if ctx is not None else None
        span_id = format(ctx.span_id, "016x") if ctx is not None else None
        return {
            "name": span.name,
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": format(parent.span_id, "016x") if parent else None,
            "start_ns": span.start_time,
            "end_ns": span.end_time,
            "duration_ns": (span.end_time or 0) - (span.start_time or 0),
            "status": span.status.status_code.name if span.status else None,
            "attributes": attrs,
            "events": [
                {
                    "name": e.name,
                    "ts_ns": e.timestamp,
                    "attributes": dict(e.attributes or {}),
                }
                for e in span.events
            ],
        }


def configure_fleet_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    configure_telemetry()


def configure_telemetry(*, force: bool = False) -> bool:
    """Initialize Logfire for local-only span capture.

    Idempotent — safe to call from every CLI entrypoint. Returns True if
    telemetry is now wired, False if disabled or unavailable. Disabled when
    ``AGENT_FLEET_TELEMETRY=0`` or when ``logfire`` import fails.
    """
    global _CONFIGURED
    with _LOCK:
        if _CONFIGURED and not force:
            return True
        if os.environ.get("AGENT_FLEET_TELEMETRY", "1") == "0":
            return False
        try:
            import logfire
        except ImportError:
            logger.warning("logfire not installed; telemetry disabled")
            return False

        base_dir = _default_telemetry_dir()
        processor = _JsonlSpanProcessor(base_dir)
        logfire.configure(
            send_to_logfire=False,
            console=False,
            inspect_arguments=False,
            additional_span_processors=[processor],
            service_name="agent-fleet",
        )
        _CONFIGURED = True
        logger.info("telemetry → %s", _today_jsonl(base_dir))
        return True


def span(name: str, **attributes: object) -> AbstractContextManager[object]:
    """Context manager wrapper that no-ops cleanly if logfire isn't configured."""
    try:
        import logfire
    except ImportError:
        from contextlib import nullcontext

        return nullcontext()
    from typing import Any, cast

    return cast("Any", logfire).span(name, **attributes)
