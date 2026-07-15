"""Grok Build CLI backend — unit tests (mocked subprocess; no live network)."""

from __future__ import annotations

import json
import os
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

from agent_fleet.grok_backend import (
    DEFAULT_MODEL,
    GrokBackend,
    GrokLLMResult,
    GrokSession,
    _GrokErrorSession,
    call_grok,
    check_grok_auth,
)

# --- Constants / registry -------------------------------------------------


def test_default_model_is_grok_build() -> None:
    assert DEFAULT_MODEL == "grok-4.5"


def test_grok_resolves_from_registry() -> None:
    from agent_fleet.backends import make_backend
    from agent_fleet.config import FleetConfig

    cfg = FleetConfig(default_backend="grok", default_model=None)
    backend = make_backend(cfg)
    assert isinstance(backend, GrokBackend)


def test_grok_env_var_is_none() -> None:
    from agent_fleet.backends import backend_env_var

    assert backend_env_var("grok") is None


def test_grok_auth_probe_present() -> None:
    from agent_fleet.backends import backend_auth_probe

    probe = backend_auth_probe("grok")
    assert probe is not None
    assert callable(probe)


def test_grok_factory_inherits_default_when_config_model_none() -> None:
    from agent_fleet.backends import make_backend
    from agent_fleet.config import FleetConfig

    cfg = FleetConfig(default_backend="grok", default_model=None)
    backend = make_backend(cfg)
    assert isinstance(backend, GrokBackend)
    assert backend.model == "grok-4.5"


def test_grok_factory_respects_explicit_model_and_bin() -> None:
    from agent_fleet.backends import make_backend
    from agent_fleet.config import FleetConfig

    cfg = FleetConfig(
        default_backend="grok",
        default_model="grok-code",
        grok_bin="/custom/bin/grok",
    )
    backend = make_backend(cfg)
    assert isinstance(backend, GrokBackend)
    assert backend.model == "grok-code"
    assert backend.grok_bin == "/custom/bin/grok"


# --- check_grok_auth ------------------------------------------------------


def test_check_grok_auth_fails_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("agent_fleet.grok_backend.shutil.which", lambda _: None)
    monkeypatch.setattr("agent_fleet.grok_backend.LOCAL_BIN", tmp_path / "nope")
    monkeypatch.setattr("agent_fleet.grok_backend.AUTH_JSON", tmp_path / "auth.json")
    ok, detail, fix = check_grok_auth()
    assert ok is False
    assert "binary" in detail.lower()
    assert fix


def test_check_grok_auth_fails_when_auth_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bin_path = tmp_path / "grok"
    bin_path.write_text("#!/bin/sh\n", encoding="utf-8")
    bin_path.chmod(0o755)
    monkeypatch.setattr("agent_fleet.grok_backend.shutil.which", lambda _: str(bin_path))
    monkeypatch.setattr("agent_fleet.grok_backend.LOCAL_BIN", tmp_path / "missing-local")
    monkeypatch.setattr("agent_fleet.grok_backend.AUTH_JSON", tmp_path / "auth.json")
    ok, detail, fix = check_grok_auth()
    assert ok is False
    assert "missing" in detail.lower() or "auth" in detail.lower()
    assert "grok login" in fix


def test_check_grok_auth_fails_when_auth_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bin_path = tmp_path / "grok"
    bin_path.write_text("#!/bin/sh\n", encoding="utf-8")
    bin_path.chmod(0o755)
    auth = tmp_path / "auth.json"
    auth.write_text("   \n", encoding="utf-8")
    monkeypatch.setattr("agent_fleet.grok_backend.shutil.which", lambda _: str(bin_path))
    monkeypatch.setattr("agent_fleet.grok_backend.LOCAL_BIN", tmp_path / "missing-local")
    monkeypatch.setattr("agent_fleet.grok_backend.AUTH_JSON", auth)
    ok, detail, fix = check_grok_auth()
    assert ok is False
    assert "empty" in detail.lower()
    assert "grok login" in fix


def test_check_grok_auth_fails_when_auth_invalid_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bin_path = tmp_path / "grok"
    bin_path.write_text("#!/bin/sh\n", encoding="utf-8")
    bin_path.chmod(0o755)
    auth = tmp_path / "auth.json"
    auth.write_text("not-json", encoding="utf-8")
    monkeypatch.setattr("agent_fleet.grok_backend.shutil.which", lambda _: str(bin_path))
    monkeypatch.setattr("agent_fleet.grok_backend.LOCAL_BIN", tmp_path / "missing-local")
    monkeypatch.setattr("agent_fleet.grok_backend.AUTH_JSON", auth)
    ok, detail, fix = check_grok_auth()
    assert ok is False
    assert "invalid" in detail.lower()
    assert "grok login" in fix


def test_check_grok_auth_fails_when_auth_not_object(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bin_path = tmp_path / "grok"
    bin_path.write_text("#!/bin/sh\n", encoding="utf-8")
    bin_path.chmod(0o755)
    auth = tmp_path / "auth.json"
    auth.write_text("[]", encoding="utf-8")
    monkeypatch.setattr("agent_fleet.grok_backend.shutil.which", lambda _: str(bin_path))
    monkeypatch.setattr("agent_fleet.grok_backend.LOCAL_BIN", tmp_path / "missing-local")
    monkeypatch.setattr("agent_fleet.grok_backend.AUTH_JSON", auth)
    ok, detail, _fix = check_grok_auth()
    assert ok is False
    assert "json object" in detail.lower() or "non-empty" in detail.lower()


def test_check_grok_auth_passes_with_valid_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bin_path = tmp_path / "grok"
    bin_path.write_text("#!/bin/sh\n", encoding="utf-8")
    bin_path.chmod(0o755)
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"https://auth.x.ai::id": {"key": "tok"}}), encoding="utf-8")
    monkeypatch.setattr("agent_fleet.grok_backend.shutil.which", lambda _: str(bin_path))
    monkeypatch.setattr("agent_fleet.grok_backend.LOCAL_BIN", tmp_path / "missing-local")
    monkeypatch.setattr("agent_fleet.grok_backend.AUTH_JSON", auth)
    ok, detail, fix = check_grok_auth()
    assert ok is True
    assert "authenticated" in detail.lower()
    assert fix == ""


# --- call_grok argv -------------------------------------------------------


def _fake_completed(stdout: str = "ok", returncode: int = 0) -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = ""
    return m


def test_call_grok_builds_yolo_argv(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def _run(cmd: list[str], **kwargs: Any) -> MagicMock:  # noqa: ANN401
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return _fake_completed("hello")

    with patch("agent_fleet.grok_backend.subprocess.run", side_effect=_run):
        out = call_grok(
            "do it",
            work_dir=str(tmp_path),
            model="grok-4.5",
            grok_bin="/bin/grok",
            mode="agent",
        )
    assert out == "hello"
    cmd = captured["cmd"]
    assert cmd[0] == "/bin/grok"
    assert "--no-auto-update" in cmd
    assert "--cwd" in cmd and str(tmp_path) in cmd
    assert "--prompt-file" in cmd
    assert "--output-format" in cmd and "plain" in cmd
    assert "-m" in cmd and "grok-4.5" in cmd
    assert "--yolo" in cmd
    assert "--permission-mode" not in cmd
    # Fleet must not inject XAI_API_KEY
    env = captured["env"]
    assert env is not None
    # May be present from parent env, but call_grok must not force-set a new key
    # beyond os.environ.copy() — verify we did not add a sentinel.
    assert env.get("XAI_API_KEY") == os.environ.get("XAI_API_KEY")


def test_call_grok_plan_mode_uses_permission_mode(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def _run(cmd: list[str], **_kwargs: Any) -> MagicMock:  # noqa: ANN401
        captured["cmd"] = cmd
        return _fake_completed("plan")

    with patch("agent_fleet.grok_backend.subprocess.run", side_effect=_run):
        call_grok("plan it", work_dir=str(tmp_path), grok_bin="/bin/grok", mode="plan")
    cmd = captured["cmd"]
    assert "--permission-mode" in cmd
    assert "plan" in cmd
    assert "--yolo" not in cmd


def test_call_grok_session_flags(tmp_path: Path) -> None:
    captured: list[list[str]] = []

    def _run(cmd: list[str], **_kwargs: Any) -> MagicMock:  # noqa: ANN401
        captured.append(list(cmd))
        return _fake_completed("ok")

    with patch("agent_fleet.grok_backend.subprocess.run", side_effect=_run):
        call_grok(
            "first",
            work_dir=str(tmp_path),
            grok_bin="/bin/grok",
            session_id="11111111-1111-1111-1111-111111111111",
            resume=False,
        )
        call_grok(
            "second",
            work_dir=str(tmp_path),
            grok_bin="/bin/grok",
            session_id="11111111-1111-1111-1111-111111111111",
            resume=True,
        )
    assert "-s" in captured[0]
    assert "11111111-1111-1111-1111-111111111111" in captured[0]
    assert "-r" in captured[1]
    assert "11111111-1111-1111-1111-111111111111" in captured[1]


def test_call_grok_raises_on_nonzero(tmp_path: Path) -> None:
    with patch(
        "agent_fleet.grok_backend.subprocess.run",
        return_value=_fake_completed(stdout="", returncode=1),
    ):
        m = MagicMock()
        m.returncode = 1
        m.stdout = ""
        m.stderr = "boom"
        with (
            patch("agent_fleet.grok_backend.subprocess.run", return_value=m),
            pytest.raises(RuntimeError, match="grok failed"),
        ):
            call_grok("x", work_dir=str(tmp_path), grok_bin="/bin/grok")


# --- GrokBackend.run / create_session -------------------------------------


def test_run_returns_error_when_auth_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agent_fleet.grok_backend.check_grok_auth",
        lambda: (False, "no auth", "run `grok login`"),
    )
    backend = GrokBackend(grok_bin="/bin/grok")
    result = backend.run("do something", max_tokens=100, timeout_s=30, cwd=tmp_path)
    assert isinstance(result, GrokLLMResult)
    assert result.exit_code == 1
    assert "no auth" in result.stderr
    assert result.stdout == ""


def test_run_success_and_scope_note(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agent_fleet.grok_backend.check_grok_auth",
        lambda: (True, "ok", ""),
    )
    captured: dict[str, Any] = {}

    def _fake_call(prompt: str, **kwargs: Any) -> str:  # noqa: ANN401
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        return "done"

    monkeypatch.setattr("agent_fleet.grok_backend.call_grok", _fake_call)
    backend = GrokBackend(grok_bin="/bin/grok", model="grok-4.5")
    result = backend.run(
        "fix the bug",
        max_tokens=100,
        timeout_s=30,
        cwd=tmp_path,
        allowed_tools=["path:src/", "path:tests/"],
    )
    assert result.exit_code == 0
    assert result.stdout == "done"
    assert "Hard scope constraint" in captured["prompt"]
    assert "src/" in captured["prompt"]
    assert "tests/" in captured["prompt"]


def test_session_first_send_uses_s_second_uses_r(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def _fake_call(prompt: str, **kwargs: Any) -> str:  # noqa: ANN401
        calls.append({"prompt": prompt, **kwargs})
        return f"reply-{len(calls)}"

    monkeypatch.setattr("agent_fleet.grok_backend.call_grok", _fake_call)
    session = GrokSession(
        grok_bin="/bin/grok",
        model="grok-4.5",
        cwd=tmp_path,
        session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    )
    r1 = session.send("one", max_tokens=10, timeout_s=30)
    r2 = session.send("two", max_tokens=10, timeout_s=30)
    assert r1.exit_code == 0 and r2.exit_code == 0
    assert calls[0]["session_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert calls[0]["resume"] is False
    assert calls[1]["resume"] is True
    assert session.agent_id == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_create_session_error_when_auth_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "agent_fleet.grok_backend.check_grok_auth",
        lambda: (False, "missing auth", "run `grok login`"),
    )
    backend = GrokBackend(grok_bin="/bin/grok")
    session = backend.create_session(persona_name="coder", cwd=tmp_path)
    assert isinstance(session, _GrokErrorSession)
    result = session.send("hi", max_tokens=1, timeout_s=1)
    assert result.exit_code == 1
    assert "missing auth" in result.stderr


def test_create_session_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "agent_fleet.grok_backend.check_grok_auth",
        lambda: (True, "ok", ""),
    )
    backend = GrokBackend(grok_bin="/bin/grok")
    session = backend.create_session(persona_name="coder", cwd=tmp_path, mode="plan")
    assert isinstance(session, GrokSession)
    assert session.agent_id is not None


def _swap_backend_modules(mod_names: tuple[str, ...]) -> dict[str, ModuleType | None]:
    """Pop backend modules (and package attrs) so make_backend re-imports cleanly.

    Returns a restore map for :func:`_restore_backend_modules`.
    """
    import agent_fleet

    saved: dict[str, ModuleType | None] = {m: sys.modules.get(m) for m in mod_names}
    for m in mod_names:
        sys.modules.pop(m, None)
        short = m.rsplit(".", 1)[-1]
        if hasattr(agent_fleet, short):
            delattr(agent_fleet, short)
    return saved


def _restore_backend_modules(saved: dict[str, ModuleType | None]) -> None:
    """Restore sys.modules *and* ``agent_fleet.<name>`` attrs after isolation tests.

    Restoring only ``sys.modules`` leaves a stale package attribute pointing at the
    re-imported module; subsequent monkeypatch strings like
    ``agent_fleet.grok_backend.X`` then patch the wrong object.
    """
    import agent_fleet

    for m in saved:
        sys.modules.pop(m, None)
        short = m.rsplit(".", 1)[-1]
        if hasattr(agent_fleet, short):
            delattr(agent_fleet, short)
    for m, obj in saved.items():
        short = m.rsplit(".", 1)[-1]
        if obj is not None:
            sys.modules[m] = obj
            setattr(agent_fleet, short, obj)


def test_import_isolation_grok_does_not_import_others() -> None:
    _backend_mods = (
        "agent_fleet.cursor_backend",
        "agent_fleet.kimi_backend",
        "agent_fleet.openrouter_backend",
        "agent_fleet.grok_backend",
    )
    saved = _swap_backend_modules(_backend_mods)
    try:
        from agent_fleet.backends import make_backend
        from agent_fleet.config import FleetConfig

        cfg = FleetConfig(default_backend="grok", default_model=None)
        make_backend(cfg)

        assert "agent_fleet.grok_backend" in sys.modules
        assert "agent_fleet.cursor_backend" not in sys.modules
        assert "agent_fleet.kimi_backend" not in sys.modules
        assert "agent_fleet.openrouter_backend" not in sys.modules
    finally:
        _restore_backend_modules(saved)
