"""Tests for agent_fleet.tool_env — PATH augmentation and pre-commit ensure."""

from __future__ import annotations

import os
from subprocess import CompletedProcess
from typing import TYPE_CHECKING
from unittest.mock import patch

from agent_fleet.tool_env import augment_path, ensure_pre_commit, which_tool

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_augment_path_prepends_local_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local = tmp_path / ".local" / "bin"
    local.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin")
    env = augment_path({"PATH": "/usr/bin", "HOME": str(tmp_path)})
    parts = env["PATH"].split(os.pathsep)
    assert str(local) in parts
    assert parts.index(str(local)) < parts.index("/usr/bin")


def test_which_tool_finds_binary_in_local_bin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = tmp_path / ".local" / "bin"
    local.mkdir(parents=True)
    tool = local / "pre-commit"
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    # Strip PATH so only extra_bin_dirs / which_tool logic can find it.
    monkeypatch.setenv("PATH", "/usr/bin")
    found = which_tool("pre-commit")
    assert found == str(tool)


def test_ensure_pre_commit_returns_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = tmp_path / ".local" / "bin"
    local.mkdir(parents=True)
    tool = local / "pre-commit"
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(local))
    assert ensure_pre_commit(install=False) == str(tool)


def test_ensure_pre_commit_installs_via_uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local = tmp_path / ".local" / "bin"
    local.mkdir(parents=True)
    uv = local / "uv"
    uv.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    uv.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(local) + os.pathsep + "/usr/bin")

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> CompletedProcess[str]:
        calls.append(list(cmd))
        # Simulate uv tool install creating pre-commit
        pc = local / "pre-commit"
        pc.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        pc.chmod(0o755)
        return CompletedProcess(cmd, 0, "", "")

    with patch("agent_fleet.tool_env.which_tool") as mock_which:
        # First calls: pre-commit missing, uv present; after install, pre-commit found.
        state = {"n": 0}

        def side_effect(name: str, env: object | None = None) -> str | None:
            del env  # interface parity with which_tool
            state["n"] += 1
            if name == "pre-commit":
                pc = local / "pre-commit"
                return str(pc) if pc.exists() else None
            if name == "uv":
                return str(uv)
            return None

        mock_which.side_effect = side_effect
        with patch("agent_fleet.tool_env.subprocess.run", side_effect=fake_run):
            found = ensure_pre_commit(install=True)
    assert found == str(local / "pre-commit")
    assert calls and calls[0][0] == str(uv)
    assert "tool" in calls[0] and "install" in calls[0]
