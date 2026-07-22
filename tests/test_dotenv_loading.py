"""cli._load_dotenv_file / _load_dotenv_files: minimal .env loader for the fleet CLI."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from agent_fleet.cli import _load_dotenv_file, _load_dotenv_files, _parse_dotenv_line

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_parse_dotenv_line_basic() -> None:
    assert _parse_dotenv_line("FOO=bar") == ("FOO", "bar")


def test_parse_dotenv_line_blank_and_comments() -> None:
    assert _parse_dotenv_line("") is None
    assert _parse_dotenv_line("   ") is None
    assert _parse_dotenv_line("# a comment") is None
    assert _parse_dotenv_line("   # indented comment") is None


def test_parse_dotenv_line_quotes_stripped() -> None:
    assert _parse_dotenv_line('FOO="bar"') == ("FOO", "bar")
    assert _parse_dotenv_line("FOO='bar'") == ("FOO", "bar")
    assert _parse_dotenv_line('FOO="bar baz"') == ("FOO", "bar baz")


def test_parse_dotenv_line_export_prefix() -> None:
    assert _parse_dotenv_line("export FOO=bar") == ("FOO", "bar")
    assert _parse_dotenv_line('export FOO="bar"') == ("FOO", "bar")


def test_parse_dotenv_line_malformed_returns_none() -> None:
    assert _parse_dotenv_line("no_equals_sign_here") is None
    assert _parse_dotenv_line("=noname") is None
    assert _parse_dotenv_line("1BAD=value") is None


def test_load_dotenv_file_sets_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLEET_TEST_FAKE_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# comment",
                "",
                "FLEET_TEST_FAKE_KEY=abc123fake",
                'FLEET_TEST_QUOTED="quoted-fake-value"',
                "export FLEET_TEST_EXPORTED=exported-fake-value",
                "this line is malformed",
            ]
        ),
        encoding="utf-8",
    )
    _load_dotenv_file(env_file)
    try:
        assert os.environ["FLEET_TEST_FAKE_KEY"] == "abc123fake"
        assert os.environ["FLEET_TEST_QUOTED"] == "quoted-fake-value"
        assert os.environ["FLEET_TEST_EXPORTED"] == "exported-fake-value"
    finally:
        for key in ("FLEET_TEST_FAKE_KEY", "FLEET_TEST_QUOTED", "FLEET_TEST_EXPORTED"):
            monkeypatch.delenv(key, raising=False)


def test_load_dotenv_file_does_not_override_existing_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLEET_TEST_FAKE_KEY", "real-env-value")
    env_file = tmp_path / ".env"
    env_file.write_text("FLEET_TEST_FAKE_KEY=from-dotenv-fake\n", encoding="utf-8")
    _load_dotenv_file(env_file)
    assert os.environ["FLEET_TEST_FAKE_KEY"] == "real-env-value"


def test_load_dotenv_file_missing_is_noop(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist" / ".env"
    # Must not raise.
    _load_dotenv_file(missing)


def test_load_dotenv_files_loads_cwd_and_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd_dir = tmp_path / "cwd"
    workspace_dir = tmp_path / "workspace"
    cwd_dir.mkdir()
    workspace_dir.mkdir()
    (cwd_dir / ".env").write_text("FLEET_TEST_CWD_KEY=cwd-fake-value\n", encoding="utf-8")
    (workspace_dir / ".env").write_text(
        "FLEET_TEST_WORKSPACE_KEY=workspace-fake-value\n", encoding="utf-8"
    )
    monkeypatch.delenv("FLEET_TEST_CWD_KEY", raising=False)
    monkeypatch.delenv("FLEET_TEST_WORKSPACE_KEY", raising=False)
    monkeypatch.chdir(cwd_dir)
    _load_dotenv_files(str(workspace_dir))
    try:
        assert os.environ["FLEET_TEST_CWD_KEY"] == "cwd-fake-value"
        assert os.environ["FLEET_TEST_WORKSPACE_KEY"] == "workspace-fake-value"
    finally:
        monkeypatch.delenv("FLEET_TEST_CWD_KEY", raising=False)
        monkeypatch.delenv("FLEET_TEST_WORKSPACE_KEY", raising=False)
