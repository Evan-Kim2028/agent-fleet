"""Verify local OpenTelemetry capture writes spans to JSONL."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from agent_fleet import telemetry

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _reset_configured() -> Iterator[None]:
    """Ensure configure_telemetry runs fresh per test."""
    telemetry._CONFIGURED = False
    yield
    telemetry._CONFIGURED = False


def test_span_writes_jsonl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_TELEMETRY_DIR", str(tmp_path))
    monkeypatch.delenv("AGENT_FLEET_TELEMETRY", raising=False)

    assert telemetry.configure_telemetry(force=True) is True

    with (
        telemetry.span("fleet.dispatch", run_id="r1", issue_number=42),
        telemetry.span("cursor.run", model="composer-2.5", fast="false"),
    ):
        pass

    import logfire

    logfire.force_flush()

    jsonl_files = list(tmp_path.glob("spans-*.jsonl"))
    assert len(jsonl_files) == 1, f"expected one jsonl, found {jsonl_files}"
    lines = jsonl_files[0].read_text().strip().splitlines()
    spans = [json.loads(line) for line in lines]
    names = [s["name"] for s in spans]
    assert "fleet.dispatch" in names
    assert "cursor.run" in names

    parents = {s["span_id"]: s.get("parent_span_id") for s in spans}
    cursor_span = next(s for s in spans if s["name"] == "cursor.run")
    dispatch_span = next(s for s in spans if s["name"] == "fleet.dispatch")
    assert parents[cursor_span["span_id"]] == dispatch_span["span_id"]

    attrs = dispatch_span["attributes"]
    assert attrs.get("run_id") == "r1"
    assert attrs.get("issue_number") == 42


def test_telemetry_disabled_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_TELEMETRY", "0")
    assert telemetry.configure_telemetry(force=True) is False
