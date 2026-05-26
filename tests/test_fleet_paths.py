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


def test_migrate_from_hermes_copies_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hermes = tmp_path / ".hermes" / "coding_fleet"
    hermes.mkdir(parents=True)
    (hermes / "fleet.yaml").write_text("max_parallel: 2\n", encoding="utf-8")
    af_home = tmp_path / ".agent-fleet"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_FLEET_HOME", str(af_home))
    monkeypatch.setattr(fleet_paths, "_LEGACY_HERMES_CONFIG", hermes / "fleet.yaml")
    monkeypatch.setattr(fleet_paths, "_LEGACY_HERMES_RUNS", tmp_path / ".hermes" / "fleet" / "runs")

    actions = fleet_paths.migrate_from_hermes()
    assert "fleet.yaml" in actions
    assert (af_home / "fleet.yaml").read_text(encoding="utf-8") == "max_parallel: 2\n"
