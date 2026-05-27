"""bridge.session.start event and raw bridge passthrough log tests."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from agent_fleet.cursor_backend import CursorBackend
from agent_fleet.observability.context import bind_run
from agent_fleet.observability.events import RunContext
from agent_fleet.observability.log import RunLog
from agent_fleet.observability.sinks import MemoryRingSink

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    fake = MagicMock()
    fake.Agent.create.return_value = MagicMock(
        agent_id="agent-bridge-1",
        send=MagicMock(side_effect=lambda *_a, **_kw: _make_run()),
        dispose=MagicMock(),
        close=MagicMock(),
    )
    fake.StdioMcpServerConfig = lambda **kw: ("stdio", kw)
    fake.HttpMcpServerConfig = lambda **kw: ("http", kw)
    fake.LocalAgentOptions = lambda **kw: ("local", kw)
    fake.AgentOptions = lambda **kw: ("agentopts", kw)
    fake.SendOptions = lambda **kw: ("sendopts", kw)
    monkeypatch.setitem(__import__("sys").modules, "cursor_sdk", fake)
    return fake


def _make_run(
    result: str = "ok",
    status: str = "finished",
    agent_id: str = "agent-bridge-1",
) -> MagicMock:
    terminal = MagicMock(result=result, agent_id=agent_id, status=status)
    run = MagicMock()
    run.events.return_value = iter([])
    run.wait.return_value = terminal
    run._terminal_result = terminal
    return run


def _make_run_log(run_id: str) -> tuple[RunLog, MemoryRingSink]:
    sink = MemoryRingSink()
    log = RunLog(
        run_id=run_id,
        context=RunContext(run_id=run_id, phase="IMPLEMENT"),
        sinks=[sink],
    )
    return log, sink


def test_bridge_session_start_emitted_on_create_session(
    fake_sdk: MagicMock,  # noqa: ARG001
    tmp_path: Path,
) -> None:
    """Creating a CursorSession while a RunLog is bound emits bridge.session.start."""
    run_log, sink = _make_run_log("r-bridge-1")
    with bind_run(run_log, run_log.context):
        backend = CursorBackend(api_key="x")
        sess = backend.create_session(persona_name="coder", cwd=tmp_path)

    assert sess.agent_id == "agent-bridge-1"
    start_events = [e for e in sink.events if e.event == "bridge.session.start"]
    assert len(start_events) == 1
    data = start_events[0].data
    assert data["agent_id"] == "agent-bridge-1"
    assert data["run_id"] == "r-bridge-1"
    assert data["workspace"] == str(tmp_path)


def test_bridge_session_start_not_emitted_without_run_log(
    fake_sdk: MagicMock,  # noqa: ARG001
    tmp_path: Path,
) -> None:
    """Without a bound RunLog, bridge.session.start is silently skipped."""
    backend = CursorBackend(api_key="x")
    # No bind_run context — should not raise
    sess = backend.create_session(persona_name="coder", cwd=tmp_path)
    assert sess.agent_id == "agent-bridge-1"


def test_bridge_passthrough_log_written_on_events(
    fake_sdk: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raw SDK events are appended to <run_id>.bridge.jsonl in the runs dir."""
    import json

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(runs_dir))

    # Rebuild the module-level default so it picks up the new env var.
    import agent_fleet.fleet_paths as fp

    monkeypatch.setattr(fp, "default_runs_dir", lambda: runs_dir)
    import agent_fleet.cursor_backend as cb

    monkeypatch.setattr(cb, "default_runs_dir", lambda: runs_dir)

    run_id = "dispatch-99-abcd1234"
    run_log, _sink = _make_run_log(run_id)

    class _FakeEvent:
        sdk_message = None
        interaction_update = None
        type = "fake-event"

    terminal = MagicMock(result="done", agent_id="agent-z", status="finished")
    run = MagicMock()
    run.events.return_value = iter([_FakeEvent()])
    run.wait.return_value = terminal
    run._terminal_result = terminal
    fake_sdk.Agent.create.return_value.send = MagicMock(return_value=run)

    with bind_run(run_log, run_log.context):
        backend = CursorBackend(api_key="x")
        sess = backend.create_session(persona_name="coder", cwd=tmp_path)
        sess.send("do work", max_tokens=0, timeout_s=0)

    bridge_path = runs_dir / f"{run_id}.bridge.jsonl"
    assert bridge_path.exists(), f"Expected {bridge_path} to exist"
    lines = bridge_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 1
    record = json.loads(lines[0])
    assert record["run_id"] == run_id
    assert "ts" in record
    assert "event" in record
