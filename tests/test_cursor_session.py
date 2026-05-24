"""Tests for CursorSession lifecycle and MCP forwarding (fake SDK)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from agent_fleet.contracts.mcp import HttpMcpServerSpec, StdioMcpServerSpec
from agent_fleet.cursor_backend import CursorBackend, CursorLLMResult

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch the cursor_sdk import inside CursorBackend to a fake module."""
    fake = MagicMock()
    fake.Agent.create.return_value = MagicMock(
        agent_id="agent-xyz",
        send=MagicMock(return_value=MagicMock(
            result="ok output", agent_id="agent-xyz", status="finished",
        )),
        dispose=MagicMock(),
    )
    fake.StdioMcpServerConfig = lambda **kw: ("stdio", kw)
    fake.HttpMcpServerConfig = lambda **kw: ("http", kw)
    fake.LocalAgentOptions = lambda **kw: ("local", kw)
    fake.AgentOptions = lambda **kw: ("agentopts", kw)
    monkeypatch.setitem(__import__("sys").modules, "cursor_sdk", fake)
    return fake


def test_create_session_forwards_mcp_servers(
    fake_sdk: MagicMock, tmp_path: Path,
) -> None:
    backend = CursorBackend(api_key="x")
    sess = backend.create_session(
        persona_name="coder",
        cwd=tmp_path,
        mcp_servers={
            "playwright": StdioMcpServerSpec(command="npx", args=("-y", "x")),
            "context7": HttpMcpServerSpec(url="https://x", headers={"A": "B"}),
        },
    )
    assert sess.agent_id == "agent-xyz"
    args, _kwargs = fake_sdk.Agent.create.call_args
    # Agent.create receives AgentOptions(...) positionally; our fake records
    # them as a ("agentopts", kw) tuple.
    assert len(args) == 1
    tag, opts = args[0]
    assert tag == "agentopts"
    assert "mcp_servers" in opts
    assert set(opts["mcp_servers"]) == {"playwright", "context7"}


def test_session_send_returns_cursor_llm_result(
    fake_sdk: MagicMock,  # noqa: ARG001  # fixture monkeypatches cursor_sdk
    tmp_path: Path,
) -> None:
    backend = CursorBackend(api_key="x")
    sess = backend.create_session(persona_name="coder", cwd=tmp_path)
    result = sess.send("do work", max_tokens=1000, timeout_s=60)
    assert isinstance(result, CursorLLMResult)
    assert result.exit_code == 0
    assert result.stdout == "ok output"
    assert result.agent_id == "agent-xyz"


def test_session_send_maps_error_status_to_nonzero_exit(
    fake_sdk: MagicMock, tmp_path: Path,
) -> None:
    fake_sdk.Agent.create.return_value.send.return_value = MagicMock(
        result="partial", agent_id="agent-xyz", status="expired",
    )
    backend = CursorBackend(api_key="x")
    sess = backend.create_session(persona_name="coder", cwd=tmp_path)
    result = sess.send("hi", max_tokens=1, timeout_s=1)
    assert result.exit_code == 1
    assert "expired" in result.stderr


def test_session_dispose_calls_sdk_dispose(
    fake_sdk: MagicMock, tmp_path: Path,
) -> None:
    backend = CursorBackend(api_key="x")
    sess = backend.create_session(persona_name="coder", cwd=tmp_path)
    sess.dispose()
    sess.dispose()  # idempotent
    fake_sdk.Agent.create.return_value.dispose.assert_called_once()


def test_create_session_returns_error_session_without_api_key(
    tmp_path: Path,
) -> None:
    backend = CursorBackend(api_key="")
    sess = backend.create_session(persona_name="coder", cwd=tmp_path)
    result = sess.send("x", max_tokens=1, timeout_s=1)
    assert result.exit_code == 1
    assert "CURSOR_API_KEY" in result.stderr
