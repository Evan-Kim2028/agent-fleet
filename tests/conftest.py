"""Shared pytest fixtures for agent_fleet tests."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agent_fleet.config import load_fleet_config
from agent_fleet.hooks import FleetTask
from agent_fleet.personas import YamlPersonaResolver
from agent_fleet.runner import TaskRunner

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_fleet.hooks import LLMBackend

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def fleet_config_with_session_backend(
    tmp_path: Path,
) -> Callable[[LLMBackend], TaskRunner]:
    """Return a factory that builds a TaskRunner wired with the given backend.

    The runner is pre-configured with a single FleetTask (persona="coder",
    pipeline="simple") and uses tmp_path as the workspace so tests can call
    runner.run(task_id=..., pipeline=...) without real filesystem setup.
    """
    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    resolver = YamlPersonaResolver(fleet_config)
    task = FleetTask(
        goal="Test task",
        context="",
        persona="coder",
        workspace=str(tmp_path),
        pipeline="simple",
    )

    def _make_runner(backend: LLMBackend) -> TaskRunner:
        return TaskRunner(
            backend=backend,
            fleet_config=fleet_config,
            persona_resolver=resolver,
            task=task,
            workspace=tmp_path,
        )

    return _make_runner
