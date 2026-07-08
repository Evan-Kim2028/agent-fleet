"""OpenRouter backend — mocked-HTTP unit tests + a live test gated on OPENROUTER_API_KEY.

The live test is skipped unless ``OPENROUTER_API_KEY`` is set (e.g. via a local
gitignored ``.env``), so CI never burns real tokens. The mocked tests cover the
request shape, auth header, response parsing, error paths, and the missing-key
guard — enough to prove the backend is a correct LLMBackend adapter without
network.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from agent_fleet.openrouter_backend import (
    _MAX_CORRECTIONS,
    _MAX_HISTORY_CHARS,
    _MAX_RETRIES,
    DEFAULT_MODEL,
    OPENROUTER_BASE_URL,
    OpenRouterBackend,
    OpenRouterLLMResult,
    OpenRouterSession,
    _claims_completion_without_tools,
    _command_violates_scope,
    _execute_tool,
    _is_repetitive,
    _is_within_scope,
    _normalize_openrouter_usage,
    _OpenRouterErrorSession,
    _parse_text_tool_calls,
    _safe_resolve,
)

# --- Registry resolution -------------------------------------------------


def test_openrouter_resolves_from_registry() -> None:
    from agent_fleet.backends import make_backend
    from agent_fleet.config import FleetConfig

    cfg = FleetConfig(default_backend="openrouter", default_model=None)
    backend = make_backend(cfg)
    assert isinstance(backend, OpenRouterBackend)


def test_openrouter_env_var_contract() -> None:
    from agent_fleet.backends import backend_env_var

    assert backend_env_var("openrouter") == "OPENROUTER_API_KEY"


def test_openrouter_default_model_is_hy3_free() -> None:
    assert DEFAULT_MODEL == "tencent/hy3:free"


def test_openrouter_factory_inherits_default_when_config_model_none() -> None:
    from agent_fleet.backends import make_backend
    from agent_fleet.config import FleetConfig

    cfg = FleetConfig(default_backend="openrouter", default_model=None)
    backend = make_backend(cfg)
    assert isinstance(backend, OpenRouterBackend)
    assert backend.model == "tencent/hy3:free"


def test_openrouter_factory_respects_explicit_model() -> None:
    from agent_fleet.backends import make_backend
    from agent_fleet.config import FleetConfig

    cfg = FleetConfig(default_backend="openrouter", default_model="anthropic/claude-3.5-sonnet")
    backend = make_backend(cfg)
    assert isinstance(backend, OpenRouterBackend)
    assert backend.model == "anthropic/claude-3.5-sonnet"


# --- Missing-key guard ---------------------------------------------------


def test_run_returns_error_when_api_key_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    backend = OpenRouterBackend(api_key="")
    result = backend.run("do something", max_tokens=100, timeout_s=30, cwd=tmp_path)
    assert isinstance(result, OpenRouterLLMResult)
    assert result.exit_code == 1
    assert "OPENROUTER_API_KEY" in result.stderr
    assert result.stdout == ""


# --- Mocked HTTP: success path ------------------------------------------


def _fake_urlopen_response(payload: dict[str, Any]) -> MagicMock:
    """A context-manager-ish MagicMock matching urllib.request.urlopen's shape."""
    cm = MagicMock()
    cm.__enter__ = MagicMock(
        return_value=MagicMock(read=MagicMock(return_value=json.dumps(payload).encode("utf-8")))
    )
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def test_run_parses_chat_completion_response(tmp_path: Path) -> None:
    payload = {
        "id": "gen-abc123",
        "choices": [{"message": {"content": "Hello from hy3"}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
    }
    backend = OpenRouterBackend(api_key="sk-or-test", model="tencent/hy3:free")
    with patch(
        "agent_fleet.openrouter_backend.urllib.request.urlopen",
        return_value=_fake_urlopen_response(payload),
    ) as mock_open:
        result = backend.run("say hello", max_tokens=50, timeout_s=30, cwd=tmp_path)

    assert result.exit_code == 0
    assert result.stdout == "Hello from hy3"
    assert result.stderr == ""
    assert result.agent_id == "gen-abc123"
    assert result.usage == {
        "input_tokens": 12,
        "output_tokens": 5,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    assert result.duration_s >= 0.0

    # Verify the request shape: URL, method, auth header, body.
    mock_open.assert_called_once()
    request = mock_open.call_args[0][0]
    assert request.full_url == f"{OPENROUTER_BASE_URL}/chat/completions"
    assert request.get_method() == "POST"
    assert request.headers["Authorization"] == "Bearer sk-or-test"
    body = json.loads(request.data.decode("utf-8"))
    assert body["model"] == "tencent/hy3:free"
    assert body["messages"] == [{"role": "user", "content": "say hello"}]
    assert body["max_tokens"] == 50


def test_run_includes_scope_note_when_allowed_tools_given(tmp_path: Path) -> None:
    payload = {"id": "gen-1", "choices": [{"message": {"content": "ok"}}]}
    backend = OpenRouterBackend(api_key="sk-or-test")
    captured: dict[str, Any] = {}

    def _capture(req: urllib.request.Request, timeout: int) -> MagicMock:  # noqa: ARG001
        data = req.data
        assert isinstance(data, bytes)
        captured["body"] = json.loads(data.decode("utf-8"))
        return _fake_urlopen_response(payload)

    with patch("agent_fleet.openrouter_backend.urllib.request.urlopen", side_effect=_capture):
        backend.run(
            "fix the bug",
            max_tokens=100,
            timeout_s=30,
            cwd=tmp_path,
            allowed_tools=["path:src/", "path:tests/"],
        )

    content = captured["body"]["messages"][0]["content"]
    assert "Hard scope constraint" in content
    assert "src/" in content
    assert "tests/" in content


def test_run_omits_max_tokens_when_zero(tmp_path: Path) -> None:
    payload = {"id": "gen-1", "choices": [{"message": {"content": "ok"}}]}
    backend = OpenRouterBackend(api_key="sk-or-test")
    captured: dict[str, Any] = {}

    def _capture(req: urllib.request.Request, timeout: int) -> MagicMock:  # noqa: ARG001
        data = req.data
        assert isinstance(data, bytes)
        captured["body"] = json.loads(data.decode("utf-8"))
        return _fake_urlopen_response(payload)

    with patch("agent_fleet.openrouter_backend.urllib.request.urlopen", side_effect=_capture):
        backend.run("prompt", max_tokens=0, timeout_s=30, cwd=tmp_path)

    assert "max_tokens" not in captured["body"]


# --- Mocked HTTP: error paths -------------------------------------------


def test_run_handles_http_error(tmp_path: Path) -> None:
    import email.message
    import urllib.error

    backend = OpenRouterBackend(api_key="sk-or-test")
    err = urllib.error.HTTPError(
        url=f"{OPENROUTER_BASE_URL}/chat/completions",
        code=429,
        msg="Too Many Requests",
        hdrs=email.message.Message(),
        fp=None,
    )
    with patch("agent_fleet.openrouter_backend.urllib.request.urlopen", side_effect=err):
        result = backend.run("prompt", max_tokens=10, timeout_s=30, cwd=tmp_path)

    assert result.exit_code == 1
    assert "429" in result.stderr


def test_run_handles_url_error(tmp_path: Path) -> None:
    import urllib.error

    backend = OpenRouterBackend(api_key="sk-or-test")
    err = urllib.error.URLError("connection refused")
    with patch("agent_fleet.openrouter_backend.urllib.request.urlopen", side_effect=err):
        result = backend.run("prompt", max_tokens=10, timeout_s=30, cwd=tmp_path)

    assert result.exit_code == 1
    assert "transport error" in result.stderr


def test_run_handles_non_json_response(tmp_path: Path) -> None:
    cm = MagicMock()
    cm.__enter__ = MagicMock(
        return_value=MagicMock(read=MagicMock(return_value=b"<html>not json</html>"))
    )
    cm.__exit__ = MagicMock(return_value=False)
    backend = OpenRouterBackend(api_key="sk-or-test")
    with patch("agent_fleet.openrouter_backend.urllib.request.urlopen", return_value=cm):
        result = backend.run("prompt", max_tokens=10, timeout_s=30, cwd=tmp_path)

    assert result.exit_code == 1
    assert "non-JSON" in result.stderr


# --- Live test (gated on OPENROUTER_API_KEY) -----------------------------


def test_live_openrouter_hy3_call(tmp_path: Path) -> None:
    """Live end-to-end call against tencent/hy3:free. Skipped without a key."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        pytest.skip("OPENROUTER_API_KEY not set — skipping live OpenRouter test")

    backend = OpenRouterBackend(api_key=key, model="tencent/hy3:free")
    result = backend.run(
        "Reply with exactly the word: pong",
        max_tokens=500,
        timeout_s=60,
        cwd=tmp_path,
    )
    assert result.exit_code == 0, f"live call failed: {result.stderr}"
    assert result.stdout, "live call returned empty stdout"
    assert result.duration_s > 0.0


# --- Session / tool-use loop ---------------------------------------------
#
# These tests mock ``_call_openrouter_raw`` (the low-level HTTP function the
# session loops over) so no network is involved. Each ``side_effect`` is a list
# of responses consumed in order, simulating the multi-turn tool-calling loop.


def _tool_call_response(
    tool_name: str,
    args_dict: dict[str, Any],
    *,
    call_id: str = "call-1",
    content: str | None = None,
) -> dict[str, Any]:
    """Build an OpenRouter chat-completion response that requests a tool call."""
    return {
        "id": "gen-test-123",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(args_dict),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 30},
        },
    }


def _stop_response(content: str = "Done.") -> dict[str, Any]:
    """Build an OpenRouter chat-completion response that ends the loop."""
    return {
        "id": "gen-test-456",
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def test_create_session_returns_openrouter_session(tmp_path: Path) -> None:
    backend = OpenRouterBackend(api_key="sk-or-test")
    session = backend.create_session(persona_name="coder", cwd=tmp_path)
    assert isinstance(session, OpenRouterSession)


def test_create_session_returns_error_session_when_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    backend = OpenRouterBackend(api_key="")
    session = backend.create_session(persona_name="coder", cwd=tmp_path)
    assert isinstance(session, _OpenRouterErrorSession)


def test_error_session_send_returns_error() -> None:
    session = _OpenRouterErrorSession("OPENROUTER_API_KEY is not set")
    result = session.send("do something", max_tokens=100, timeout_s=30)
    assert isinstance(result, OpenRouterLLMResult)
    assert result.exit_code == 1
    assert "OPENROUTER_API_KEY" in result.stderr
    assert result.stdout == ""


def test_session_send_tool_use_loop(tmp_path: Path) -> None:
    # Create a real file for read_file to read.
    (tmp_path / "hello.txt").write_text("hello world", encoding="utf-8")

    backend = OpenRouterBackend(api_key="sk-or-test")
    session = backend.create_session(persona_name="coder", cwd=tmp_path)
    responses = [
        _tool_call_response("read_file", {"path": "hello.txt"}),
        _stop_response("I read the file."),
    ]
    with patch(
        "agent_fleet.openrouter_backend._call_openrouter_raw",
        side_effect=responses,
    ):
        result = session.send("read hello.txt", max_tokens=100, timeout_s=30)

    assert result.exit_code == 0
    assert result.stdout == "I read the file."
    assert "read_file" in result.mcp_tool_calls


def test_session_send_write_file_creates_file(tmp_path: Path) -> None:
    backend = OpenRouterBackend(api_key="sk-or-test")
    session = backend.create_session(persona_name="coder", cwd=tmp_path)
    responses = [
        _tool_call_response("write_file", {"path": "out.txt", "content": "hello world"}),
        _stop_response("File written."),
    ]
    with patch(
        "agent_fleet.openrouter_backend._call_openrouter_raw",
        side_effect=responses,
    ):
        result = session.send("write out.txt", max_tokens=100, timeout_s=30)

    assert result.exit_code == 0
    written = (tmp_path / "out.txt").read_text(encoding="utf-8")
    assert written == "hello world"
    assert "write_file" in result.mcp_tool_calls


def test_execute_tool_list_files_sorts_mixed_entries_without_raising(tmp_path: Path) -> None:
    # Regression test: list_files used to sort raw dicts (unorderable in
    # Python 3), raising a TypeError that escaped _execute_tool and killed
    # the whole session. A directory with 2+ entries of mixed type/name
    # order must sort cleanly by (type, name) instead.
    (tmp_path / "zeta.txt").write_text("z", encoding="utf-8")
    (tmp_path / "alpha.txt").write_text("a", encoding="utf-8")
    (tmp_path / "subdir").mkdir()

    result = _execute_tool("list_files", {"path": "."}, cwd=tmp_path, scope_prefixes=["."])
    payload = json.loads(result)

    assert "error" not in payload
    entries = payload["entries"]
    names = [e["name"] for e in entries]
    assert set(names) == {"zeta.txt", "alpha.txt", "subdir"}
    # Sorted by (type, name): dirs before files, alphabetically within type.
    assert entries == sorted(entries, key=lambda e: (e["type"], e["name"]))


def test_execute_tool_catches_handler_exception_and_returns_tool_error() -> None:
    with patch(
        "agent_fleet.openrouter_backend._execute_tool_inner",
        side_effect=RuntimeError("boom"),
    ):
        result = _execute_tool("read_file", {"path": "x"}, cwd=None, scope_prefixes=["."])

    payload = json.loads(result)
    assert "error" in payload
    assert "tool error: read_file raised RuntimeError: boom" in payload["error"]


def test_session_send_continues_after_tool_handler_exception(tmp_path: Path) -> None:
    # If a tool handler raises, the session loop must not propagate the
    # exception — it should feed a tool-error result back to the model and
    # continue to the next iteration.
    backend = OpenRouterBackend(api_key="sk-or-test")
    session = backend.create_session(persona_name="coder", cwd=tmp_path)
    responses = [
        _tool_call_response("read_file", {"path": "hello.txt"}),
        _stop_response("Recovered after the tool error."),
    ]
    with (
        patch(
            "agent_fleet.openrouter_backend._call_openrouter_raw",
            side_effect=responses,
        ),
        patch(
            "agent_fleet.openrouter_backend._execute_tool_inner",
            side_effect=RuntimeError("boom"),
        ),
    ):
        result = session.send("read hello.txt", max_tokens=100, timeout_s=30)

    assert result.exit_code == 0
    assert result.stdout == "Recovered after the tool error."
    tool_msgs = [m for m in session._messages if m.get("role") == "tool"]
    assert tool_msgs
    assert "tool error: read_file raised RuntimeError: boom" in tool_msgs[0]["content"]


def test_session_send_scope_enforcement_blocks_write(tmp_path: Path) -> None:
    backend = OpenRouterBackend(api_key="sk-or-test")
    session = backend.create_session(persona_name="coder", cwd=tmp_path)
    responses = [
        # Write outside the allowed "src/" prefix.
        _tool_call_response("write_file", {"path": "tests/bad.txt", "content": "nope"}),
        _stop_response("Done."),
    ]
    with patch(
        "agent_fleet.openrouter_backend._call_openrouter_raw",
        side_effect=responses,
    ):
        session.send(
            "write tests/bad.txt",
            max_tokens=100,
            timeout_s=30,
            allowed_tools=["path:src/"],
        )

    # The file must not have been created.
    assert not (tmp_path / "tests" / "bad.txt").exists()
    # The tool result (appended to history) should contain an error message.
    tool_msgs = [m for m in session._messages if m.get("role") == "tool"]
    assert tool_msgs
    assert "error" in tool_msgs[0]["content"]


def test_session_send_path_traversal_blocked(tmp_path: Path) -> None:
    backend = OpenRouterBackend(api_key="sk-or-test")
    session = backend.create_session(persona_name="coder", cwd=tmp_path)
    responses = [
        _tool_call_response("read_file", {"path": "../../../etc/passwd"}),
        _stop_response("Done."),
    ]
    with patch(
        "agent_fleet.openrouter_backend._call_openrouter_raw",
        side_effect=responses,
    ):
        result = session.send("read passwd", max_tokens=100, timeout_s=30)

    # The tool result should report an error (file outside workspace).
    tool_msgs = [m for m in session._messages if m.get("role") == "tool"]
    assert tool_msgs
    assert "error" in tool_msgs[0]["content"]
    # read_file was still "called" (recorded), even though it was rejected.
    assert "read_file" in result.mcp_tool_calls


def test_session_send_max_iterations_cap(tmp_path: Path) -> None:
    backend = OpenRouterBackend(api_key="sk-or-test")
    session = backend.create_session(persona_name="coder", cwd=tmp_path)
    # Always return a tool_calls response — the loop never terminates naturally.
    endless = _tool_call_response("read_file", {"path": "missing.txt"})

    with patch(
        "agent_fleet.openrouter_backend._call_openrouter_raw",
        side_effect=lambda *a, **k: endless,  # noqa: ARG005
    ):
        result = session.send("loop forever", max_tokens=100, timeout_s=30)

    assert result.exit_code == 1
    assert "max tool iterations" in result.stderr


def test_session_conversation_history_persists(tmp_path: Path) -> None:
    backend = OpenRouterBackend(api_key="sk-or-test")
    session = backend.create_session(persona_name="coder", cwd=tmp_path)
    initial_len = len(session._messages)

    with patch(
        "agent_fleet.openrouter_backend._call_openrouter_raw",
        side_effect=[_stop_response("first answer")],
    ):
        session.send("first prompt", max_tokens=100, timeout_s=30)
    after_first = len(session._messages)
    assert after_first > initial_len

    with patch(
        "agent_fleet.openrouter_backend._call_openrouter_raw",
        side_effect=[_stop_response("second answer")],
    ):
        session.send("second prompt", max_tokens=100, timeout_s=30)
    after_second = len(session._messages)
    # History grew again and retains the first turn's messages.
    assert after_second > after_first
    contents = [m.get("content") for m in session._messages]
    assert "first answer" in contents
    assert "second answer" in contents


# --- Observability: usage normalization ----------------------------------


def test_normalize_openrouter_usage_maps_fields() -> None:
    raw = {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "prompt_tokens_details": {"cached_tokens": 30},
    }
    out = _normalize_openrouter_usage(raw)
    assert out is not None
    assert out["input_tokens"] == 100
    assert out["output_tokens"] == 50
    assert out["cache_read_tokens"] == 30
    assert out["cache_write_tokens"] == 0


def test_normalize_openrouter_usage_returns_none_for_empty() -> None:
    assert _normalize_openrouter_usage(None) is None
    assert _normalize_openrouter_usage({}) is None


def test_run_emits_llm_usage_to_run_log(tmp_path: Path) -> None:
    payload = {
        "id": "gen-usage-1",
        "choices": [{"message": {"content": "ok"}}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 30},
        },
    }
    backend = OpenRouterBackend(api_key="sk-or-test")
    mock_log = MagicMock()
    with (
        patch(
            "agent_fleet.openrouter_backend._call_openrouter_raw",
            return_value=payload,
        ),
        patch("agent_fleet.openrouter_backend.get_run_log", return_value=mock_log),
    ):
        result = backend.run("prompt", max_tokens=100, timeout_s=30, cwd=tmp_path)

    assert result.exit_code == 0
    mock_log.llm_usage.assert_called_once()
    kwargs = mock_log.llm_usage.call_args.kwargs
    assert kwargs["input_tokens"] == 100
    assert kwargs["output_tokens"] == 50
    # call_openrouter returns the raw usage dict (including nested
    # prompt_tokens_details), so _normalize_openrouter_usage can extract
    # cache hits from the run() path.
    assert kwargs["cache_read_tokens"] == 30
    assert kwargs["cache_write_tokens"] == 0


# --- Path / scope helpers -------------------------------------------------


def test_safe_resolve_rejects_traversal(tmp_path: Path) -> None:
    assert _safe_resolve("../../../etc/passwd", tmp_path) is None


def test_safe_resolve_accepts_valid_path(tmp_path: Path) -> None:
    resolved = _safe_resolve("src/file.py", tmp_path)
    assert resolved is not None
    assert resolved.is_relative_to(tmp_path.resolve())


def test_is_within_scope_dot_matches_everything(tmp_path: Path) -> None:
    target = (tmp_path / "anywhere" / "deep" / "file.py").resolve()
    assert _is_within_scope(target, ["."], tmp_path) is True


def test_is_within_scope_specific_prefix(tmp_path: Path) -> None:
    src_file = (tmp_path / "src" / "mod.py").resolve()
    test_file = (tmp_path / "tests" / "test_x.py").resolve()
    assert _is_within_scope(src_file, ["src/"], tmp_path) is True
    assert _is_within_scope(test_file, ["src/"], tmp_path) is False


# --- Text-mode tool call fallback parser ---------------------------------


def test_parse_text_tool_calls_pseudo_xml() -> None:
    """Pseudo-XML format observed from tencent/hy3:free."""
    content = (
        "I'll read the target file.\n"
        "<tool_calls:abc123>\n"
        "<tool_call:abc123>read_file\n"
        "parameter: path: docs/NEW-REPO.md\n"
        "</tool_call:abc123>\n"
        "</tool_calls:abc123>"
    )
    assert _parse_text_tool_calls(content) == [("read_file", {"path": "docs/NEW-REPO.md"})]


def test_parse_text_tool_calls_json_block() -> None:
    """JSON code block format."""
    content = (
        "```json\n"
        '{"name": "write_file", "arguments": '
        '{"path": "test.txt", "content": "hello"}}\n'
        "```"
    )
    assert _parse_text_tool_calls(content) == [
        ("write_file", {"path": "test.txt", "content": "hello"})
    ]


def test_parse_text_tool_calls_returns_none_for_plain_text() -> None:
    """Normal text without tool calls returns None (genuine final answer)."""
    content = "I've completed the task. The file has been updated."
    assert _parse_text_tool_calls(content) is None


def test_parse_text_tool_calls_returns_none_for_empty() -> None:
    """Empty string returns None."""
    assert _parse_text_tool_calls("") is None


def test_parse_text_tool_calls_multiple_xml_calls() -> None:
    """Multiple pseudo-XML tool calls in one response."""
    content = (
        "<tool_calls:x>\n"
        "<tool_call:x>read_file\n"
        "parameter: path: a.txt\n"
        "</tool_call:x>\n"
        "<tool_call:x>write_file\n"
        "parameter: path: b.txt\n"
        "parameter: content: hi\n"
        "</tool_call:x>\n"
        "</tool_calls:x>"
    )
    result = _parse_text_tool_calls(content)
    assert result == [
        ("read_file", {"path": "a.txt"}),
        ("write_file", {"path": "b.txt", "content": "hi"}),
    ]
    assert len(result) == 2


def test_parse_text_tool_calls_ignores_unknown_tools() -> None:
    """A pseudo-XML block with an unknown tool name yields None."""
    content = (
        "<tool_calls:x>\n"
        "<tool_call:x>search_web\n"
        "parameter: query: hello\n"
        "</tool_call:x>\n"
        "</tool_calls:x>"
    )
    assert _parse_text_tool_calls(content) is None


def test_session_send_text_mode_tool_calls_executed(tmp_path: Path) -> None:
    """finish_reason='stop' with text-mode tool calls still executes the tool."""
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")

    first_response = {
        "id": "gen-1",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": (
                        "Reading file.\n"
                        "<tool_calls:x>\n"
                        "<tool_call:x>read_file\n"
                        "parameter: path: hello.txt\n"
                        "</tool_call:x>\n"
                        "</tool_calls:x>"
                    ),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 20},
    }
    second_response = {
        "id": "gen-1",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Done. The file contains hello.",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 80, "completion_tokens": 10},
    }

    backend = OpenRouterBackend(api_key="sk-or-test")
    session = backend.create_session(persona_name="coder", cwd=tmp_path)
    with patch(
        "agent_fleet.openrouter_backend._call_openrouter_raw",
        side_effect=[first_response, second_response],
    ):
        result = session.send("read hello.txt", max_tokens=100, timeout_s=30)

    assert result.exit_code == 0
    assert "read_file" in result.mcp_tool_calls
    assert result.stdout == "Done. The file contains hello."


# --- Repetition / hallucination guards -----------------------------------


def test_is_repetitive_detects_loop() -> None:
    content = "I'll read the target file to locate the exact lines. " * 20
    assert _is_repetitive(content) is True


def test_is_repetitive_returns_false_for_normal_text() -> None:
    content = "I read the file and made the changes. The task is complete."
    assert _is_repetitive(content) is False


def test_is_repetitive_returns_false_for_short_content() -> None:
    assert _is_repetitive("short") is False


def test_claims_completion_without_tools_detects_phrases() -> None:
    for content in (
        "Changes made: edited docs/NEW-REPO.md",
        "I edited the file",
        "Both edits are correct",
        "The task is complete",
        "Follow-up needed: None",
    ):
        assert _claims_completion_without_tools(content) is True, content


def test_claims_completion_without_tools_returns_false_for_normal() -> None:
    assert _claims_completion_without_tools("I need to read the file first.") is False


def test_session_corrective_prompt_on_hallucinated_completion(tmp_path: Path) -> None:
    # Create a real file for read_file to read on the corrective turn.
    (tmp_path / "hello.txt").write_text("hello world", encoding="utf-8")

    response_1 = {
        "id": "gen-1",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": (
                        "Changes made: Added the OpenRouter line to "
                        "docs/NEW-REPO.md. The task is complete."
                    ),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 20},
    }
    response_2 = _tool_call_response("read_file", {"path": "hello.txt"})
    response_3 = {
        "id": "gen-3",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Done. The file has been read and edited.",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 80, "completion_tokens": 10},
    }

    backend = OpenRouterBackend(api_key="sk-or-test")
    session = backend.create_session(persona_name="coder", cwd=tmp_path)
    with patch(
        "agent_fleet.openrouter_backend._call_openrouter_raw",
        side_effect=[response_1, response_2, response_3],
    ):
        result = session.send("edit docs/NEW-REPO.md", max_tokens=100, timeout_s=30)

    assert result.exit_code == 0
    assert "read_file" in result.mcp_tool_calls
    assert result.stdout == "Done. The file has been read and edited."


def test_session_corrective_prompt_on_repetition(tmp_path: Path) -> None:
    # Create a real file for read_file to read on the corrective turn.
    (tmp_path / "hello.txt").write_text("hello world", encoding="utf-8")

    response_1 = {
        "id": "gen-1",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "I'll read the target file. " * 20,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 20},
    }
    response_2 = _tool_call_response("read_file", {"path": "hello.txt"})
    response_3 = _stop_response("Done.")

    backend = OpenRouterBackend(api_key="sk-or-test")
    session = backend.create_session(persona_name="coder", cwd=tmp_path)
    with patch(
        "agent_fleet.openrouter_backend._call_openrouter_raw",
        side_effect=[response_1, response_2, response_3],
    ):
        result = session.send("read hello.txt", max_tokens=100, timeout_s=30)

    assert result.exit_code == 0
    assert "read_file" in result.mcp_tool_calls
    assert result.stdout == "Done."


def test_session_fails_after_max_corrections(tmp_path: Path) -> None:
    # Always return a hallucinated completion with no tool calls. After
    # _MAX_CORRECTIONS corrective prompts the guard gives up — but now it must
    # NOT accept the content as a successful final answer.
    hallucinated = {
        "id": "gen-loop",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Changes made. Task complete.",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 20},
    }

    backend = OpenRouterBackend(api_key="sk-or-test")
    session = backend.create_session(persona_name="coder", cwd=tmp_path)
    mock_raw = MagicMock(side_effect=lambda *a, **k: hallucinated)  # noqa: ARG005
    with patch("agent_fleet.openrouter_backend._call_openrouter_raw", new=mock_raw):
        result = session.send("edit the file", max_tokens=100, timeout_s=30)

    assert result.exit_code == 1
    assert "correction limit reached" in result.stderr
    # The content is still surfaced in stdout so it can be inspected.
    assert result.stdout == "Changes made. Task complete."
    # 3 corrective prompts + 1 final (failed) attempt.
    assert mock_raw.call_count == _MAX_CORRECTIONS + 1


# --- Retry with backoff (_call_openrouter_raw) ----------------------------


def test_retry_on_429_then_succeeds(tmp_path: Path) -> None:
    import email.message

    err = urllib.error.HTTPError(
        url=f"{OPENROUTER_BASE_URL}/chat/completions",
        code=429,
        msg="Too Many Requests",
        hdrs=email.message.Message(),
        fp=None,
    )
    payload = {"id": "gen-1", "choices": [{"message": {"content": "ok"}}]}
    backend = OpenRouterBackend(api_key="sk-or-test")

    with (
        patch(
            "agent_fleet.openrouter_backend.urllib.request.urlopen",
            side_effect=[err, _fake_urlopen_response(payload)],
        ) as mock_open,
        patch("agent_fleet.openrouter_backend._sleep") as mock_sleep,
    ):
        result = backend.run("prompt", max_tokens=10, timeout_s=30, cwd=tmp_path)

    assert result.exit_code == 0
    assert result.stdout == "ok"
    assert mock_open.call_count == 2
    mock_sleep.assert_called_once()


def test_retry_on_5xx_then_succeeds(tmp_path: Path) -> None:
    import email.message

    err = urllib.error.HTTPError(
        url=f"{OPENROUTER_BASE_URL}/chat/completions",
        code=503,
        msg="Service Unavailable",
        hdrs=email.message.Message(),
        fp=None,
    )
    payload = {"id": "gen-1", "choices": [{"message": {"content": "ok"}}]}
    backend = OpenRouterBackend(api_key="sk-or-test")

    with (
        patch(
            "agent_fleet.openrouter_backend.urllib.request.urlopen",
            side_effect=[err, err, _fake_urlopen_response(payload)],
        ),
        patch("agent_fleet.openrouter_backend._sleep") as mock_sleep,
    ):
        result = backend.run("prompt", max_tokens=10, timeout_s=30, cwd=tmp_path)

    assert result.exit_code == 0
    assert mock_sleep.call_count == 2


def test_no_retry_on_non_retryable_4xx(tmp_path: Path) -> None:
    import email.message

    err = urllib.error.HTTPError(
        url=f"{OPENROUTER_BASE_URL}/chat/completions",
        code=400,
        msg="Bad Request",
        hdrs=email.message.Message(),
        fp=None,
    )
    backend = OpenRouterBackend(api_key="sk-or-test")

    with (
        patch(
            "agent_fleet.openrouter_backend.urllib.request.urlopen", side_effect=err
        ) as mock_open,
        patch("agent_fleet.openrouter_backend._sleep") as mock_sleep,
    ):
        result = backend.run("prompt", max_tokens=10, timeout_s=30, cwd=tmp_path)

    assert result.exit_code == 1
    assert "400" in result.stderr
    mock_open.assert_called_once()
    mock_sleep.assert_not_called()


def test_retry_exhausted_raises_after_max_retries(tmp_path: Path) -> None:
    err = urllib.error.URLError("connection refused")
    backend = OpenRouterBackend(api_key="sk-or-test")

    with (
        patch(
            "agent_fleet.openrouter_backend.urllib.request.urlopen", side_effect=err
        ) as mock_open,
        patch("agent_fleet.openrouter_backend._sleep") as mock_sleep,
    ):
        result = backend.run("prompt", max_tokens=10, timeout_s=30, cwd=tmp_path)

    assert result.exit_code == 1
    assert "transport error" in result.stderr
    assert mock_open.call_count == _MAX_RETRIES + 1
    assert mock_sleep.call_count == _MAX_RETRIES


def test_retry_honors_retry_after_header(tmp_path: Path) -> None:
    err = urllib.error.HTTPError(
        url=f"{OPENROUTER_BASE_URL}/chat/completions",
        code=429,
        msg="Too Many Requests",
        hdrs={"Retry-After": "5"},
        fp=None,
    )
    payload = {"id": "gen-1", "choices": [{"message": {"content": "ok"}}]}
    backend = OpenRouterBackend(api_key="sk-or-test")

    with (
        patch(
            "agent_fleet.openrouter_backend.urllib.request.urlopen",
            side_effect=[err, _fake_urlopen_response(payload)],
        ),
        patch("agent_fleet.openrouter_backend._sleep") as mock_sleep,
    ):
        result = backend.run("prompt", max_tokens=10, timeout_s=30, cwd=tmp_path)

    assert result.exit_code == 0
    mock_sleep.assert_called_once_with(5.0)


# --- Usage logged on all exit paths ---------------------------------------


def test_session_logs_usage_on_max_iterations_exit(tmp_path: Path) -> None:
    backend = OpenRouterBackend(api_key="sk-or-test")
    session = backend.create_session(persona_name="coder", cwd=tmp_path)
    endless = _tool_call_response("read_file", {"path": "missing.txt"})
    mock_log = MagicMock()

    with (
        patch(
            "agent_fleet.openrouter_backend._call_openrouter_raw",
            side_effect=lambda *a, **k: endless,  # noqa: ARG005
        ),
        patch("agent_fleet.openrouter_backend.get_run_log", return_value=mock_log),
    ):
        result = session.send("loop forever", max_tokens=100, timeout_s=30)

    assert result.exit_code == 1
    mock_log.llm_usage.assert_called_once()


def test_session_logs_usage_on_reasoning_without_content_exit(tmp_path: Path) -> None:
    response = {
        "id": "gen-1",
        "choices": [
            {
                "message": {"role": "assistant", "content": None, "reasoning": "thinking..."},
                "finish_reason": "length",
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 20},
    }
    backend = OpenRouterBackend(api_key="sk-or-test")
    session = backend.create_session(persona_name="coder", cwd=tmp_path)
    mock_log = MagicMock()

    with (
        patch("agent_fleet.openrouter_backend._call_openrouter_raw", return_value=response),
        patch("agent_fleet.openrouter_backend.get_run_log", return_value=mock_log),
    ):
        result = session.send("do something", max_tokens=100, timeout_s=30)

    assert result.exit_code == 1
    mock_log.llm_usage.assert_called_once()


# --- Bounded conversation history -------------------------------------------


def test_trim_history_elides_old_tool_results(tmp_path: Path) -> None:
    backend = OpenRouterBackend(api_key="sk-or-test")
    session = backend.create_session(persona_name="coder", cwd=tmp_path)

    big_blob = "x" * (_MAX_HISTORY_CHARS + 1000)
    # Old tool message that should get elided, followed by plenty of recent
    # messages so it falls outside the protected "recent" window.
    session._messages.append({"role": "user", "content": "do stuff"})
    session._messages.append(
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]}
    )
    session._messages.append({"role": "tool", "tool_call_id": "c1", "content": big_blob})
    for i in range(15):
        session._messages.append({"role": "user", "content": f"filler {i}"})

    session._trim_history()

    tool_msgs = [m for m in session._messages if m.get("role") == "tool"]
    assert tool_msgs[0]["content"] == "[tool result elided to save context]"


def test_trim_history_noop_under_budget(tmp_path: Path) -> None:
    backend = OpenRouterBackend(api_key="sk-or-test")
    session = backend.create_session(persona_name="coder", cwd=tmp_path)
    session._messages.append({"role": "tool", "tool_call_id": "c1", "content": "small"})

    before = [dict(m) for m in session._messages]
    session._trim_history()

    assert session._messages == before


def test_trim_history_never_touches_recent_messages(tmp_path: Path) -> None:
    backend = OpenRouterBackend(api_key="sk-or-test")
    session = backend.create_session(persona_name="coder", cwd=tmp_path)

    big_blob = "x" * (_MAX_HISTORY_CHARS + 1000)
    # Tool message within the protected "recent" window — must survive.
    for i in range(3):
        session._messages.append({"role": "user", "content": f"filler {i}"})
    session._messages.append({"role": "tool", "tool_call_id": "recent", "content": big_blob})

    session._trim_history()

    recent_tool = next(m for m in session._messages if m.get("tool_call_id") == "recent")
    assert recent_tool["content"] == big_blob


# --- Scope-aware run_command guard ------------------------------------------


def test_command_violates_scope_blocks_rm_rf_absolute() -> None:
    reason = _command_violates_scope("rm -rf /etc/passwd", ["src/"])
    assert reason is not None
    assert "rm" in reason.lower()


def test_command_violates_scope_blocks_rm_r_parent_traversal() -> None:
    reason = _command_violates_scope("rm -r ../outside", ["src/"])
    assert reason is not None


def test_command_violates_scope_blocks_git_clean() -> None:
    reason = _command_violates_scope("git clean -fd", ["src/"])
    assert reason is not None
    assert "git clean" in reason.lower()


def test_command_violates_scope_blocks_git_reset_hard() -> None:
    reason = _command_violates_scope("git reset --hard HEAD~1", ["src/"])
    assert reason is not None
    assert "reset --hard" in reason.lower()


def test_command_violates_scope_allows_relative_rm_within_scope() -> None:
    assert _command_violates_scope("rm -rf src/build", ["src/"]) is None


def test_command_violates_scope_noop_without_scope_prefixes() -> None:
    assert _command_violates_scope("rm -rf /", []) is None


def test_command_violates_scope_noop_for_dot_scope() -> None:
    assert _command_violates_scope("rm -rf /", ["."]) is None


def test_session_run_command_blocked_by_scope_guard(tmp_path: Path) -> None:
    backend = OpenRouterBackend(api_key="sk-or-test")
    session = backend.create_session(persona_name="coder", cwd=tmp_path)
    responses = [
        _tool_call_response("run_command", {"command": "rm -rf /etc/passwd"}),
        _stop_response("Done."),
    ]
    with patch(
        "agent_fleet.openrouter_backend._call_openrouter_raw",
        side_effect=responses,
    ):
        session.send(
            "clean up",
            max_tokens=100,
            timeout_s=30,
            allowed_tools=["path:src/"],
        )

    tool_msgs = [m for m in session._messages if m.get("role") == "tool"]
    assert tool_msgs
    assert "blocked" in tool_msgs[0]["content"].lower()


def test_session_run_command_unaffected_without_scope(tmp_path: Path) -> None:
    backend = OpenRouterBackend(api_key="sk-or-test")
    session = backend.create_session(persona_name="coder", cwd=tmp_path)
    responses = [
        _tool_call_response("run_command", {"command": "echo hi"}),
        _stop_response("Done."),
    ]
    with patch(
        "agent_fleet.openrouter_backend._call_openrouter_raw",
        side_effect=responses,
    ):
        result = session.send("say hi", max_tokens=100, timeout_s=30)

    assert "run_command" in result.mcp_tool_calls
    tool_msgs = [m for m in session._messages if m.get("role") == "tool"]
    assert "blocked" not in tool_msgs[0]["content"].lower()
