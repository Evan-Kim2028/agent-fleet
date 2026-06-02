"""Regression: _execute_task's except handler must survive early failures.

An exception thrown before run_workspace and phase_results are assigned used to
make the except handler itself raise UnboundLocalError, masking the real error
and skipping the clean status="error" return. Both names are now pre-initialized
before the try, mirroring token and task_workspace.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from agent_fleet.config import load_fleet_config
from agent_fleet.dispatcher import FleetDispatcher
from agent_fleet.hooks import FleetTask

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

ROOT = Path(__file__).resolve().parent.parent


def _dispatcher() -> FleetDispatcher:
    return FleetDispatcher(config=load_fleet_config(ROOT / "fleet.example.yaml"))


def _raise(exc: Exception) -> Callable[..., object]:
    def _stub(*_args: object, **_kwargs: object) -> object:
        raise exc

    return _stub


def test_early_resolve_failure_returns_clean_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FleetDispatcher, "_resolve_workspace", _raise(RuntimeError("boom")))

    result = _dispatcher()._execute_task(0, FleetTask(goal="x"))

    assert result.status == "error"
    assert "boom" in (result.error or "")


def test_repo_config_failure_returns_clean_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FleetDispatcher, "_resolve_workspace", lambda *_: ROOT)
    monkeypatch.setattr(
        "agent_fleet.dispatcher.find_repo_config", _raise(FileNotFoundError("missing target"))
    )

    result = _dispatcher()._execute_task(0, FleetTask(goal="x"))

    assert result.status == "error"
    assert "missing target" in (result.error or "")
