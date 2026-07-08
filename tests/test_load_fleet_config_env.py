"""load_fleet_config applies AGENT_FLEET_BACKEND / AGENT_FLEET_MODEL globally."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_fleet.config import load_fleet_config

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_load_fleet_config_yaml_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "fleet.yaml"
    cfg.write_text("default_backend: grok\ndefault_model: grok-4.5\n", encoding="utf-8")
    monkeypatch.delenv("AGENT_FLEET_BACKEND", raising=False)
    monkeypatch.delenv("AGENT_FLEET_MODEL", raising=False)
    fc = load_fleet_config(cfg)
    assert fc.default_backend == "grok"
    assert fc.default_model == "grok-4.5"


def test_load_fleet_config_env_overrides_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "fleet.yaml"
    cfg.write_text("default_backend: cursor\ndefault_model: composer-2.5\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_FLEET_BACKEND", "grok")
    monkeypatch.setenv("AGENT_FLEET_MODEL", "grok-4.5")
    fc = load_fleet_config(cfg)
    assert fc.default_backend == "grok"
    assert fc.default_model == "grok-4.5"


def test_load_fleet_config_env_backend_case_normalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "fleet.yaml"
    cfg.write_text("default_backend: cursor\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_FLEET_BACKEND", "GROK")
    monkeypatch.delenv("AGENT_FLEET_MODEL", raising=False)
    fc = load_fleet_config(cfg)
    assert fc.default_backend == "grok"


def test_load_fleet_config_kwarg_beats_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "fleet.yaml"
    cfg.write_text("default_backend: cursor\ndefault_model: composer-2.5\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_FLEET_BACKEND", "kimi")
    monkeypatch.setenv("AGENT_FLEET_MODEL", "kimi-for-coding")
    fc = load_fleet_config(
        cfg,
        default_backend="grok",
        default_model="grok-4.5",
    )
    assert fc.default_backend == "grok"
    assert fc.default_model == "grok-4.5"


def test_load_fleet_config_empty_env_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "fleet.yaml"
    cfg.write_text("default_backend: openrouter\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_FLEET_BACKEND", "  ")
    monkeypatch.setenv("AGENT_FLEET_MODEL", "")
    fc = load_fleet_config(cfg)
    assert fc.default_backend == "openrouter"
