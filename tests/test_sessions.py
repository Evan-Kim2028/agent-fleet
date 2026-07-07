"""Tests for AgentSession protocol and NoopSession."""

from __future__ import annotations

from agent_fleet.contracts.mcp import StdioMcpServerSpec
from agent_fleet.noop_session import NoopLLMResult
from agent_fleet.sessions import AgentSession, NoopSession


def test_noop_session_satisfies_protocol() -> None:
    sess = NoopSession()
    assert isinstance(sess, AgentSession)
    assert sess.agent_id is None


def test_noop_session_send_returns_static_error() -> None:
    sess = NoopSession()
    result = sess.send("hi", max_tokens=100, timeout_s=10)
    assert isinstance(result, NoopLLMResult)
    assert result.exit_code == 1
    assert "NoopSession" in result.stderr


def test_noop_session_dispose_is_idempotent() -> None:
    sess = NoopSession()
    sess.dispose()
    sess.dispose()  # second call should not raise


def test_noop_session_accepts_mcp_servers_silently(
    capsys,  # noqa: ANN001
) -> None:
    sess = NoopSession(
        mcp_servers={"playwright": StdioMcpServerSpec(command="npx")},
        persona_name="coder",
    )
    sess.send("x", max_tokens=1, timeout_s=1)
    captured = capsys.readouterr()
    assert "NoopSession" in (captured.out + captured.err)
