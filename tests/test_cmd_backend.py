"""Command Code CLI backend — unit tests (mocked subprocess; no live cmd)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agent_fleet.cmd_backend import (
    DEFAULT_MODEL,
    CmdBackend,
    _parse_cmd_stream,
    apply_cmd_taste,
    call_cmd,
    check_cmd_auth,
)


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))


def test_default_model_is_longcat() -> None:
    assert DEFAULT_MODEL == "meituan/longcat-2.0:free"


def test_cmd_resolves_from_registry() -> None:
    from agent_fleet.backends import make_backend
    from agent_fleet.config import FleetConfig

    cfg = FleetConfig(default_backend="cmd", default_model=None)
    backend = make_backend(cfg)
    assert isinstance(backend, CmdBackend)
    assert backend.model == DEFAULT_MODEL


def test_cmd_env_var_is_none() -> None:
    from agent_fleet.backends import backend_env_var

    assert backend_env_var("cmd") is None


def test_cmd_auth_probe_present() -> None:
    from agent_fleet.backends import backend_auth_probe

    probe = backend_auth_probe("cmd")
    assert probe is not None
    assert callable(probe)


def test_cmd_factory_respects_bin_and_taste() -> None:
    from agent_fleet.backends import make_backend
    from agent_fleet.config import FleetConfig

    cfg = FleetConfig(
        default_backend="cmd",
        default_model="meituan/longcat-2.0:free",
        cmd_bin="/opt/cmd",
        cmd_taste="/tmp/taste.md",
    )
    backend = make_backend(cfg)
    assert isinstance(backend, CmdBackend)
    assert backend.cmd_bin == "/opt/cmd"
    assert backend.cmd_taste == "/tmp/taste.md"


def test_check_cmd_auth_fails_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("agent_fleet.cmd_backend.shutil.which", lambda _: None)
    monkeypatch.setattr(
        "agent_fleet.cmd_backend._auth_json_candidates",
        lambda: [tmp_path / "auth.json"],
    )
    ok, detail, fix = check_cmd_auth()
    assert ok is False
    assert "binary" in detail.lower()
    assert "cmd login" in fix


def test_check_cmd_auth_passes_with_valid_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bin_path = tmp_path / "cmd"
    bin_path.write_text("#!/bin/sh\n", encoding="utf-8")
    bin_path.chmod(0o755)
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"token": "x"}), encoding="utf-8")
    monkeypatch.setattr("agent_fleet.cmd_backend.shutil.which", lambda _: str(bin_path))
    monkeypatch.setattr(
        "agent_fleet.cmd_backend._auth_json_candidates",
        lambda: [auth],
    )
    ok, detail, fix = check_cmd_auth()
    assert ok is True
    assert "authenticated" in detail.lower()
    assert fix == ""


def test_parse_cmd_stream_extracts_result_and_session() -> None:
    stdout = "\n".join(
        [
            "session: 11111111-1111-1111-1111-111111111111",
            json.dumps(
                {
                    "type": "event",
                    "event": {
                        "type": "run_start",
                        "sessionId": "11111111-1111-1111-1111-111111111111",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "sessionId": "11111111-1111-1111-1111-111111111111",
                    "finalText": "hello",
                    "usage": {"inputTokens": 10, "outputTokens": 2, "cacheReadTokens": 4},
                }
            ),
        ]
    )
    final, sid, usage = _parse_cmd_stream(stdout, "")
    assert final == "hello"
    assert sid == "11111111-1111-1111-1111-111111111111"
    assert usage == {"input_tokens": 10, "output_tokens": 2, "cache_read_tokens": 4}


def test_apply_cmd_taste_symlinks(tmp_path: Path) -> None:
    src = tmp_path / "src-taste.md"
    src.write_text("- prefers tests.\n", encoding="utf-8")
    work = tmp_path / "repo"
    work.mkdir()
    dest = apply_cmd_taste(str(work), src)
    assert dest == src.resolve()
    linked = work / ".commandcode" / "taste" / "taste.md"
    assert linked.is_symlink()
    assert linked.read_text(encoding="utf-8") == "- prefers tests.\n"


def _fake_completed(stdout: str = "ok", returncode: int = 0) -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = "session: 22222222-2222-2222-2222-222222222222\n"
    return m


def test_call_cmd_builds_yolo_argv(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    result_line = json.dumps(
        {
            "type": "result",
            "finalText": "hello",
            "sessionId": "22222222-2222-2222-2222-222222222222",
            "usage": {},
        }
    )

    def _run(cmd: list[str], **kwargs: Any) -> MagicMock:  # noqa: ANN401
        captured["cmd"] = cmd
        captured["cwd"] = kwargs.get("cwd")
        captured["input"] = kwargs.get("input")
        return _fake_completed(result_line)

    taste = tmp_path / "taste.md"
    taste.write_text("- x\n", encoding="utf-8")
    with patch("agent_fleet.cmd_backend.subprocess.run", side_effect=_run):
        text, sid, _usage, code = call_cmd(
            "do it",
            work_dir=str(tmp_path),
            model="meituan/longcat-2.0:free",
            cmd_bin="/bin/cmd",
            taste_src=taste,
        )
    assert text == "hello"
    assert sid == "22222222-2222-2222-2222-222222222222"
    assert code == 0
    cmd = captured["cmd"]
    assert cmd[0] == "/bin/cmd"
    assert "-p" in cmd
    assert "--yolo" in cmd
    assert "--skip-onboarding" in cmd
    assert "taste-learning=false" in cmd
    assert "-m" in cmd and "meituan/longcat-2.0:free" in cmd
    assert captured["cwd"] == str(tmp_path)
    assert captured["input"] == "do it"


def test_call_cmd_plan_mode_skips_yolo(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def _run(cmd: list[str], **_kwargs: Any) -> MagicMock:  # noqa: ANN401
        captured["cmd"] = cmd
        return _fake_completed(json.dumps({"type": "result", "finalText": "plan"}))

    with patch("agent_fleet.cmd_backend.subprocess.run", side_effect=_run):
        call_cmd("plan it", work_dir=str(tmp_path), cmd_bin="/bin/cmd", mode="plan")
    cmd = captured["cmd"]
    assert "--permission-mode" in cmd
    assert "plan" in cmd
    assert "--yolo" not in cmd


def test_call_cmd_resume_flag(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def _run(cmd: list[str], **_kwargs: Any) -> MagicMock:  # noqa: ANN401
        captured["cmd"] = cmd
        return _fake_completed(json.dumps({"type": "result", "finalText": "ok"}))

    with patch("agent_fleet.cmd_backend.subprocess.run", side_effect=_run):
        call_cmd(
            "again",
            work_dir=str(tmp_path),
            cmd_bin="/bin/cmd",
            session_id="33333333-3333-3333-3333-333333333333",
            resume=True,
        )
    assert "--resume" in captured["cmd"]
    assert "33333333-3333-3333-3333-333333333333" in captured["cmd"]


def test_call_cmd_exit_8_is_partial_success(tmp_path: Path) -> None:
    def _run(_cmd: list[str], **_kwargs: Any) -> MagicMock:  # noqa: ANN401
        return _fake_completed(json.dumps({"type": "result", "finalText": "partial"}), returncode=8)

    with patch("agent_fleet.cmd_backend.subprocess.run", side_effect=_run):
        text, _sid, _usage, code = call_cmd("x", work_dir=str(tmp_path), cmd_bin="/bin/cmd")
    assert text == "partial"
    assert code == 8


def test_call_cmd_other_exit_raises(tmp_path: Path) -> None:
    def _run(_cmd: list[str], **_kwargs: Any) -> MagicMock:  # noqa: ANN401
        return _fake_completed("nope", returncode=3)

    with (
        patch("agent_fleet.cmd_backend.subprocess.run", side_effect=_run),
        pytest.raises(RuntimeError, match="exit 3"),
    ):
        call_cmd("x", work_dir=str(tmp_path), cmd_bin="/bin/cmd")
