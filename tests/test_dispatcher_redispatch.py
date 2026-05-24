"""End-to-end: FleetDispatcher.dispatch() should redispatch hard failures."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

    from agent_fleet.dispatcher import FleetDispatcher
    from agent_fleet.hooks import FleetTaskResult

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class FakeRunResult:
    status: str
    exit_code: int = 0
    files_modified: tuple[str, ...] = ()
    stderr: str = ""


def _make_dispatcher_for_test(*, max_redispatches: int) -> FleetDispatcher:
    from agent_fleet.config import load_fleet_config
    from agent_fleet.dispatcher import FleetDispatcher

    fc = load_fleet_config(ROOT / "fleet.example.yaml")
    fc.max_redispatches = max_redispatches
    return FleetDispatcher(config=fc)


def _dispatch_one(dispatcher: FleetDispatcher) -> FleetTaskResult:
    results = dispatcher.dispatch(
        goal="test task",
        context="",
        persona="coder",
        workspace=str(ROOT),
        pipeline="simple",
    )
    assert len(results) == 1
    return results[0]


def test_dispatch_retries_once_on_hard_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_fleet.dispatcher import FleetDispatcher

    calls: list[object] = []
    statuses = iter(
        [
            FakeRunResult(status="expired", exit_code=1, stderr="Cursor expired"),
            FakeRunResult(status="success", exit_code=0),
        ]
    )

    def fake_run_one(self, task, *, handoff=None):  # noqa: ANN001, ANN202, ARG001
        calls.append(handoff)
        return next(statuses)

    monkeypatch.setattr(FleetDispatcher, "_run_one", fake_run_one)
    dispatcher = _make_dispatcher_for_test(max_redispatches=1)
    result = _dispatch_one(dispatcher)
    assert result.status == "success"
    assert len(calls) == 2
    assert calls[0] is None
    assert calls[1] is not None  # handoff threaded through


def test_dispatch_does_not_retry_soft_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_fleet.dispatcher import FleetDispatcher

    call_count = 0

    def fake_run_one(self, task, *, handoff=None):  # noqa: ANN001, ANN202, ARG001
        nonlocal call_count
        call_count += 1
        return FakeRunResult(status="verify_failed", exit_code=0)

    monkeypatch.setattr(FleetDispatcher, "_run_one", fake_run_one)
    dispatcher = _make_dispatcher_for_test(max_redispatches=3)
    _dispatch_one(dispatcher)
    assert call_count == 1


def test_dispatch_respects_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_fleet.dispatcher import FleetDispatcher

    call_count = 0

    def fake_run_one(self, task, *, handoff=None):  # noqa: ANN001, ANN202, ARG001
        nonlocal call_count
        call_count += 1
        return FakeRunResult(status="expired", exit_code=1)

    monkeypatch.setattr(FleetDispatcher, "_run_one", fake_run_one)
    dispatcher = _make_dispatcher_for_test(max_redispatches=2)
    result = _dispatch_one(dispatcher)
    assert call_count == 3
    assert result.status == "expired"
