"""Tests for agent_fleet.fleet_paths."""

from __future__ import annotations

from typing import TYPE_CHECKING

import agent_fleet.fleet_paths as fleet_paths

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_agent_fleet_home_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "af-home"))
    assert fleet_paths.agent_fleet_home() == (tmp_path / "af-home").resolve()


def test_default_fleet_config_prefers_agent_fleet_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "af-home"
    home.mkdir()
    (home / "fleet.yaml").write_text("default_persona: coder\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_FLEET_HOME", str(home))
    monkeypatch.delenv("AGENT_FLEET_CONFIG", raising=False)
    monkeypatch.delenv("CODING_FLEET_CONFIG", raising=False)
    assert fleet_paths.default_fleet_config_path() == home / "fleet.yaml"


def test_default_runs_dir_under_agent_fleet_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "af-home"
    monkeypatch.setenv("AGENT_FLEET_HOME", str(home))
    assert fleet_paths.default_runs_dir() == home / "fleet" / "runs"
