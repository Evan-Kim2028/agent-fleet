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
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    import urllib.request
    from pathlib import Path

from agent_fleet.openrouter_backend import (
    DEFAULT_MODEL,
    OPENROUTER_BASE_URL,
    OpenRouterBackend,
    OpenRouterLLMResult,
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
    assert result.usage == {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}
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
