"""Tests for the agent-fleet migrate-home CLI subcommand."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_fleet.cli import main

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_migrate_home_dry_run_lists_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hermes = tmp_path / ".hermes" / "coding_fleet"
    hermes.mkdir(parents=True)
    (hermes / "fleet.yaml").write_text("max_parallel: 2\n", encoding="utf-8")
    af_home = tmp_path / ".agent-fleet"
    monkeypatch.setenv("AGENT_FLEET_HOME", str(af_home))
    import agent_fleet.fleet_paths as fleet_paths

    monkeypatch.setattr(fleet_paths, "_LEGACY_HERMES_CONFIG", hermes / "fleet.yaml")
    monkeypatch.setattr(fleet_paths, "_LEGACY_HERMES_RUNS", tmp_path / ".hermes" / "fleet" / "runs")

    rc = main(["migrate-home", "--dry-run"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "[dry-run]" in out
    assert "fleet.yaml" in out
    assert not (af_home / "fleet.yaml").exists()


def test_migrate_home_no_legacy_reports_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    af_home = tmp_path / ".agent-fleet"
    monkeypatch.setenv("AGENT_FLEET_HOME", str(af_home))
    import agent_fleet.fleet_paths as fleet_paths

    monkeypatch.setattr(
        fleet_paths, "_LEGACY_HERMES_CONFIG", tmp_path / "missing" / "fleet.yaml"
    )
    monkeypatch.setattr(fleet_paths, "_LEGACY_HERMES_RUNS", tmp_path / "missing" / "runs")

    rc = main(["migrate-home"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "nothing to migrate" in out
