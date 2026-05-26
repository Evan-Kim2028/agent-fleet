"""Tests for external target repo configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

from agent_fleet.repo import find_repo_config, fleet_state_root, iter_target_repos, load_repo_config


def test_load_target_config_workspace_and_state_root(tmp_path: Path) -> None:
    controller = tmp_path / "controller"
    target = tmp_path / "target"
    controller.mkdir()
    target.mkdir()
    (target / ".git").mkdir()
    target_config = controller / "targets" / "app.agent-fleet.yaml"
    target_config.parent.mkdir()
    target_config.write_text(
        yaml.safe_dump(
            {
                "workspace": str(target),
                "state_root": str(controller),
                "name": "app",
                "issue_dispatch": {"enabled": True},
            }
        ),
        encoding="utf-8",
    )
    (controller / ".agent-fleet.yaml").write_text(
        yaml.safe_dump({"targets": [{"config": "targets/app.agent-fleet.yaml"}]}),
        encoding="utf-8",
    )

    repo = find_repo_config(controller)
    assert repo is not None
    assert len(repo.target_configs) == 1
    target_repo = repo.target_configs[0]
    assert target_repo.repo_root == target.resolve()
    assert target_repo.config_root == controller / "targets"
    assert fleet_state_root(target_repo) == controller.resolve()
    assert iter_target_repos(repo) == [target_repo]


def test_resolve_repo_config_honors_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    config = tmp_path / "fleet" / "targets" / "x.agent-fleet.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        yaml.safe_dump({"workspace": str(target), "name": "x"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_FLEET_TARGET_CONFIG", str(config))
    loaded = load_repo_config(config)
    assert loaded.repo_root == target.resolve()
