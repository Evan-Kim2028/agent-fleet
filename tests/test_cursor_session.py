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

    def _make_run(
        *,
        result: str = "ok output",
        status: str = "finished",
        agent_id: str = "agent-xyz",
    ) -> MagicMock:
        terminal = MagicMock(result=result, agent_id=agent_id, status=status)
        run = MagicMock()
        run.events.return_value = iter([])
        run.wait.return_value = terminal
        run._terminal_result = terminal
        return run

    fake.Agent.create.return_value = MagicMock(
        agent_id="agent-xyz",
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


def test_create_session_forwards_mcp_servers(
    fake_sdk: MagicMock,
    tmp_path: Path,
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
    assert result.mcp_tool_calls == ()


def test_consume_run_events_logs_mcp_tool_calls(
    fake_sdk: MagicMock,  # noqa: ARG001
    caplog: pytest.LogCaptureFixture,
) -> None:
    from agent_fleet.cursor_backend import _consume_run_events

    class FakeMsg:
        def __init__(self, **kwargs: object) -> None:
            self.__dict__.update(kwargs)

    class FakeEvent:
        def __init__(self, msg: FakeMsg) -> None:
            self.sdk_message = msg

    class FakeRun:
        def __init__(self) -> None:
            self._terminal_result = MagicMock(result="done", status="finished")
            self._events = [
                FakeEvent(
                    FakeMsg(
                        type="tool_call",
                        name="mcp",
                        status="running",
                        call_id="c1",
                        agent_id="agent-xyz",
                        run_id="run-1",
                        args={
                            "providerIdentifier": "playwright",
                            "toolName": "browser_navigate",
                            "args": {"url": "https://example.com"},
                        },
                    )
                ),
                FakeEvent(
                    FakeMsg(
                        type="tool_call",
                        name="mcp",
                        status="completed",
                        call_id="c1",
                        agent_id="agent-xyz",
                        run_id="run-1",
                        args={
                            "providerIdentifier": "playwright",
                            "toolName": "browser_navigate",
                            "args": {"url": "https://example.com"},
                        },
                    )
                ),
            ]

        def events(self):  # noqa: ANN202
            yield from self._events

        def wait(self):  # noqa: ANN202
            return self._terminal_result

    with caplog.at_level("INFO", logger="agent_fleet.mcp"):
        labels, usage = _consume_run_events(
            FakeRun(),
            expected_mcp_servers=frozenset({"playwright"}),
        )

    assert labels == ("playwright.browser_navigate",)
    assert usage is None
    started = "MCP tool call started: playwright.browser_navigate"
    completed = "MCP tool call completed: playwright.browser_navigate"
    assert any(started in r.message for r in caplog.records)
    assert any(completed in r.message for r in caplog.records)


def test_consume_run_events_warns_when_no_mcp_tools_used(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from agent_fleet.cursor_backend import _consume_run_events

    class FakeRun:
        _terminal_result = MagicMock(result="done", status="finished")

        def events(self):  # noqa: ANN202
            return iter([])

        def wait(self):  # noqa: ANN202
            return self._terminal_result

    with caplog.at_level("WARNING", logger="agent_fleet.mcp"):
        labels, usage = _consume_run_events(
            FakeRun(),
            expected_mcp_servers=frozenset({"playwright"}),
            warn_if_unused=True,
        )

    assert labels == ()
    assert usage is None
    assert any("no MCP tool calls observed" in r.message for r in caplog.records)


def test_consume_run_events_harvests_turn_ended_usage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The bridge emits an interaction_update with type='turn-ended' carrying
    a camelCase usage dict; the consumer must normalize to snake_case and
    surface it on the returned tuple."""
    from agent_fleet.cursor_backend import _consume_run_events

    class FakeUpdate:
        def __init__(self, type_: str, usage: dict | None = None) -> None:
            self.type = type_
            self.usage = usage

    class FakeEvent:
        def __init__(self, *, interaction_update=None) -> None:  # noqa: ANN001
            self.interaction_update = interaction_update
            self.sdk_message = None

    class FakeRun:
        def __init__(self) -> None:
            self._events = [
                FakeEvent(interaction_update=FakeUpdate("text-delta")),
                FakeEvent(interaction_update=FakeUpdate("token-delta")),
                FakeEvent(
                    interaction_update=FakeUpdate(
                        "turn-ended",
                        usage={
                            "inputTokens": 1234,
                            "outputTokens": 56,
                            "cacheReadTokens": 789,
                            "cacheWriteTokens": 0,
                        },
                    )
                ),
            ]

        def events(self):  # noqa: ANN202
            yield from self._events

        def wait(self):  # noqa: ANN202
            return MagicMock(result="ok", status="finished")

    with caplog.at_level("INFO", logger="agent_fleet.mcp"):
        labels, usage = _consume_run_events(FakeRun())

    assert labels == ()
    assert usage == {
        "input_tokens": 1234,
        "output_tokens": 56,
        "cache_read_tokens": 789,
        "cache_write_tokens": 0,
    }


def test_session_send_threads_usage_onto_cursor_llm_result(
    monkeypatch: pytest.MonkeyPatch,
    fake_sdk: MagicMock,
    tmp_path: Path,
) -> None:
    """Full session.send loop with a fake bridge emitting turn-ended must
    populate CursorLLMResult.usage so downstream callers can log it."""

    class FakeUpdate:
        def __init__(self, type_: str, usage: dict | None = None) -> None:
            self.type = type_
            self.usage = usage

    class FakeEvent:
        def __init__(self, *, interaction_update=None) -> None:  # noqa: ANN001
            self.interaction_update = interaction_update
            self.sdk_message = None

    def _make_run() -> MagicMock:
        terminal = MagicMock(result="ok output", agent_id="agent-xyz", status="finished")
        run = MagicMock()
        run.events.return_value = iter(
            [
                FakeEvent(
                    interaction_update=FakeUpdate(
                        "turn-ended",
                        usage={
                            "inputTokens": 100,
                            "outputTokens": 7,
                            "cacheReadTokens": 22,
                            "cacheWriteTokens": 3,
                        },
                    )
                ),
            ]
        )
        run.wait.return_value = terminal
        run._terminal_result = terminal
        return run

    fake_sdk.Agent.create.return_value.send = MagicMock(
        side_effect=lambda *_a, **_kw: _make_run(),
    )
    monkeypatch.setattr("agent_fleet.cursor_backend.get_run_log", lambda: None)

    backend = CursorBackend(api_key="x")
    sess = backend.create_session(persona_name="coder", cwd=tmp_path)
    result = sess.send("do work", max_tokens=1, timeout_s=1)
    assert result.exit_code == 0
    assert result.usage == {
        "input_tokens": 100,
        "output_tokens": 7,
        "cache_read_tokens": 22,
        "cache_write_tokens": 3,
    }


def test_runlog_llm_usage_accumulates_per_phase_then_rollup() -> None:
    """RunLog.llm_usage emits one event per call and accumulates totals;
    task_usage_rollup flushes a single summary keyed by phase."""
    from agent_fleet.observability.context import bind_phase
    from agent_fleet.observability.events import RunContext
    from agent_fleet.observability.log import RunLog
    from agent_fleet.observability.sinks import MemoryRingSink

    sink = MemoryRingSink()
    log = RunLog(
        run_id="r-test",
        context=RunContext(run_id="r-test"),
        sinks=[sink],
    )
    with bind_phase("PLAN"):
        log.llm_usage(
            phase="PLAN",
            model="composer-2.5",
            duration_s=1.5,
            input_tokens=100,
            output_tokens=10,
            cache_read_tokens=5,
            cache_write_tokens=0,
        )
    with bind_phase("RESEARCH"):
        log.llm_usage(
            phase="RESEARCH",
            model="composer-2.5",
            duration_s=2.0,
            input_tokens=200,
            output_tokens=15,
            cache_read_tokens=20,
            cache_write_tokens=1,
        )
    rollup = log.task_usage_rollup(task_id=0)
    assert rollup is not None
    assert rollup["calls"] == 2
    assert rollup["totals"] == {
        "input_tokens": 300,
        "output_tokens": 25,
        "cache_read_tokens": 25,
        "cache_write_tokens": 1,
    }
    assert rollup["by_phase"]["PLAN"]["calls"] == 1
    assert rollup["by_phase"]["RESEARCH"]["input_tokens"] == 200

    events = [e.event for e in sink.events]
    assert events.count("llm.usage") == 2
    assert events.count("llm.usage.task_rollup") == 1


def test_session_send_hard_fails_when_mcp_required_but_unused(
    fake_sdk: MagicMock,  # noqa: ARG001
    tmp_path: Path,
) -> None:
    backend = CursorBackend(api_key="x")
    sess = backend.create_session(
        persona_name="frontend",
        cwd=tmp_path,
        mcp_servers={
            "playwright": StdioMcpServerSpec(
                command="npx",
                args=("-y", "@playwright/mcp@latest"),
            ),
        },
    )
    result = sess.send("fix layout", max_tokens=256, timeout_s=60, expect_mcp_tools=True)
    assert result.exit_code == 1
    assert "Playwright MCP" in result.stderr or "MCP" in result.stderr
    assert result.mcp_tool_calls == ()


def test_session_send_populates_cause_when_sdk_raises(
    fake_sdk: MagicMock,
    tmp_path: Path,
) -> None:
    """When the underlying SDK send() raises, the swallow path must record the
    original exception on CursorLLMResult.cause so callers (the planner) can
    surface a real diagnostic instead of an empty stdout."""
    boom = RuntimeError("upstream transport timeout")
    agent = MagicMock(
        agent_id="agent-xyz",
        send=MagicMock(side_effect=boom),
        dispose=MagicMock(),
    )
    fake_sdk.Agent.create.return_value = agent
    backend = CursorBackend(api_key="x")
    sess = backend.create_session(persona_name="coder", cwd=tmp_path)
    result = sess.send("hi", max_tokens=1, timeout_s=1)
    assert result.exit_code == 1
    assert result.cause is boom
    assert "RuntimeError" in result.stderr
    assert "upstream transport timeout" in result.stderr


def test_session_send_maps_error_status_to_nonzero_exit(
    fake_sdk: MagicMock,
    tmp_path: Path,
) -> None:
    terminal = MagicMock(result="partial", agent_id="agent-xyz", status="expired")
    run = MagicMock()
    run.events.return_value = iter([])
    run.wait.return_value = terminal
    run._terminal_result = terminal
    agent = MagicMock(agent_id="agent-xyz", send=MagicMock(return_value=run), dispose=MagicMock())
    fake_sdk.Agent.create.return_value = agent
    backend = CursorBackend(api_key="x")
    sess = backend.create_session(persona_name="coder", cwd=tmp_path)
    result = sess.send("hi", max_tokens=1, timeout_s=1)
    assert result.exit_code == 1
    assert "expired" in result.stderr


def test_session_dispose_calls_sdk_dispose(
    fake_sdk: MagicMock,
    tmp_path: Path,
) -> None:
    backend = CursorBackend(api_key="x")
    sess = backend.create_session(persona_name="coder", cwd=tmp_path)
    sess.dispose()
    sess.dispose()  # idempotent
    fake_sdk.Agent.create.return_value.dispose.assert_called_once()


def test_session_dispose_force_kills_playwright_when_last_session(
    fake_sdk: MagicMock,  # noqa: ARG001
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_fleet.memory import McpCleanupResult, PlaywrightSessionRegistry

    PlaywrightSessionRegistry._active = 0
    cleanup_calls: list[dict[str, object]] = []

    def _cleanup(**kwargs: object) -> McpCleanupResult:
        cleanup_calls.append(kwargs)
        return McpCleanupResult(
            before=2,
            after=0,
            waited_s=0.5,
            force_killed=(123,),
            cleaned=True,
        )

    monkeypatch.setattr(
        "agent_fleet.memory.cleanup_playwright_mcp_processes",
        _cleanup,
    )
    monkeypatch.setattr("agent_fleet.memory.count_playwright_mcp_processes", lambda: 2)

    backend = CursorBackend(api_key="x")
    sess = backend.create_session(
        persona_name="frontend",
        cwd=tmp_path,
        mcp_servers={
            "playwright": StdioMcpServerSpec(
                command="npx",
                args=("-y", "@playwright/mcp@latest"),
            ),
        },
    )
    sess.dispose()

    assert cleanup_calls
    assert cleanup_calls[0]["force_kill"] is True
    assert PlaywrightSessionRegistry.active_count() == 0


def test_create_session_returns_error_session_without_api_key(
    tmp_path: Path,
) -> None:
    backend = CursorBackend(api_key="")
    sess = backend.create_session(persona_name="coder", cwd=tmp_path)
    # When CURSOR_API_KEY env var is also set, the real SDK may create a
    # working session despite empty api_key.  Skip the test in that case.
    if not hasattr(sess, "_message"):
        pytest.skip("Real Cursor SDK session created despite empty api_key (env var present)")
    result = sess.send("x", max_tokens=1, timeout_s=1)
    assert result.exit_code == 1
    assert "CURSOR_API_KEY" in result.stderr
