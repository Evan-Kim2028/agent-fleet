"""FleetDispatcher must open one AgentSession per task and forward MCP servers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from agent_fleet.cursor_backend import CursorLLMResult

if TYPE_CHECKING:
    import pytest

ROOT = Path(__file__).resolve().parent.parent


class _SessionBackend:
    """Backend exposing create_session — dispatcher should detect and use it."""

    def __init__(self) -> None:
        self.session = MagicMock()
        self.session.agent_id = "agent-test"
        self.session.send.return_value = CursorLLMResult(
            stdout="ok", stderr="", exit_code=0, duration_s=0.1, agent_id="agent-test"
        )
        self.create_session_calls: list[dict] = []
        self.run_calls: list[dict] = []

    def create_session(self, **kwargs) -> MagicMock:  # noqa: ANN003
        self.create_session_calls.append(kwargs)
        return self.session

    def run(self, prompt, **kwargs) -> CursorLLMResult:  # noqa: ANN001, ANN003, ARG002
        self.run_calls.append(kwargs)
        return CursorLLMResult(
            stdout="ok", stderr="", exit_code=0, duration_s=0.1, agent_id=None
        )


def _build_dispatcher(backend, tmp_path: Path):  # noqa: ANN001, ANN202
    from agent_fleet.config import load_fleet_config
    from agent_fleet.dispatcher import FleetDispatcher

    fc = load_fleet_config(ROOT / "fleet.example.yaml")
    fc.default_workspace = str(tmp_path)
    dispatcher = FleetDispatcher(config=fc)
    dispatcher.backend = backend  # type: ignore[assignment]
    return dispatcher


def test_dispatch_opens_session_and_forwards_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dispatcher must call backend.create_session() exactly once per task."""
    from agent_fleet.contracts.mcp import StdioMcpServerSpec

    backend = _SessionBackend()
    dispatcher = _build_dispatcher(backend, tmp_path)
    dispatcher.config.mcp_servers = {
        "playwright": StdioMcpServerSpec(command="npx", args=("-y", "@playwright/mcp@latest")),
    }
    dispatcher.config.personas["coder"].mcp_servers = ["playwright"]

    # Pretend the workspace is already a git repo so isolation logic short-circuits.
    monkeypatch.setattr(
        "agent_fleet.dispatcher.should_isolate_worktree", lambda *_a, **_k: False
    )

    results = dispatcher.dispatch(
        goal="smoke test",
        persona="coder",
        workspace=str(tmp_path),
        pipeline="simple",
    )
    assert len(results) == 1
    assert len(backend.create_session_calls) == 1, (
        f"Expected exactly one create_session call, got {len(backend.create_session_calls)}"
    )
    call = backend.create_session_calls[0]
    assert call["persona_name"] == "coder"
    assert "playwright" in call["mcp_servers"], (
        f"Expected playwright in mcp_servers, got {list(call['mcp_servers'])}"
    )
    assert backend.session.dispose.call_count == 1
    # Session.send should be the path used, not backend.run
    assert backend.session.send.call_count >= 1
    assert len(backend.run_calls) == 0, (
        "Expected backend.run NOT called when session is available, got "
        f"{len(backend.run_calls)} calls"
    )


def test_dispatch_falls_back_to_backend_run_without_create_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backends without create_session must still work via the legacy backend.run() path."""
    from agent_fleet.config import load_fleet_config
    from agent_fleet.dispatcher import FleetDispatcher

    legacy = MagicMock(spec=["run"])
    legacy.run.return_value = CursorLLMResult(
        stdout="ok", stderr="", exit_code=0, duration_s=0.1, agent_id=None
    )
    fc = load_fleet_config(ROOT / "fleet.example.yaml")
    fc.default_workspace = str(tmp_path)
    dispatcher = FleetDispatcher(config=fc)
    dispatcher.backend = legacy  # type: ignore[assignment]

    monkeypatch.setattr(
        "agent_fleet.dispatcher.should_isolate_worktree", lambda *_a, **_k: False
    )

    results = dispatcher.dispatch(
        goal="legacy", persona="coder", workspace=str(tmp_path), pipeline="simple",
    )
    assert len(results) == 1
    assert legacy.run.call_count >= 1
    assert not hasattr(legacy, "create_session")
