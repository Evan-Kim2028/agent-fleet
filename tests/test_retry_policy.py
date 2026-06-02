"""Regression tests for the failure-mode-aware RetryPolicy.

Tests that FAIL without the RetryPolicy change and pass with it:
- scope_violation is never retried (terminal status)
- transient failures retry within budget
- default policy matches prior retry counts exactly
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_fleet.redispatch import RetryPolicy, dispatch_with_retry

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class FakeResult:
    status: str
    exit_code: int = 0
    files_modified: tuple[str, ...] = ()
    stderr: str = ""


# ---------------------------------------------------------------------------
# RetryPolicy.should_retry unit tests
# ---------------------------------------------------------------------------


def test_scope_violation_is_never_retried() -> None:
    policy = RetryPolicy(max_attempts=5)
    result = FakeResult(status="scope_violation", exit_code=1)
    # attempt 0 — still has budget, but must not retry because it is terminal
    assert policy.should_retry(result, attempt=0) is False


def test_transient_failure_retries_within_budget() -> None:
    policy = RetryPolicy(max_attempts=3)
    result = FakeResult(status="expired", exit_code=1)
    assert policy.should_retry(result, attempt=0) is True
    assert policy.should_retry(result, attempt=1) is True
    # attempt 2 is the last allowed (0-based, max_attempts=3)
    assert policy.should_retry(result, attempt=2) is False


def test_soft_failure_is_not_retried() -> None:
    policy = RetryPolicy(max_attempts=5)
    result = FakeResult(status="verify_failed", exit_code=0)
    assert policy.should_retry(result, attempt=0) is False


def test_success_is_not_retried() -> None:
    policy = RetryPolicy(max_attempts=5)
    result = FakeResult(status="success", exit_code=0)
    assert policy.should_retry(result, attempt=0) is False


@pytest.mark.parametrize(
    "status",
    ["error", "cancelled", "expired", "timeout", "pipeline_nonzero"],
)
def test_hard_transient_statuses_retry_within_budget(status: str) -> None:
    policy = RetryPolicy(max_attempts=2)
    result = FakeResult(status=status, exit_code=1)
    assert policy.should_retry(result, attempt=0) is True
    assert policy.should_retry(result, attempt=1) is False


# ---------------------------------------------------------------------------
# from_max_redispatches matches legacy behavior
# ---------------------------------------------------------------------------


def test_from_max_redispatches_maps_budget_correctly() -> None:
    policy = RetryPolicy.from_max_redispatches(1)
    assert policy.max_attempts == 2

    policy3 = RetryPolicy.from_max_redispatches(3)
    assert policy3.max_attempts == 4


def test_default_policy_matches_prior_retry_counts() -> None:
    """dispatch_with_retry with legacy max_redispatches=2 must call dispatch 3 times."""
    calls = 0

    def fake_dispatch(task, *, handoff=None):  # noqa: ANN001, ANN202, ARG001
        nonlocal calls
        calls += 1
        return FakeResult(status="expired", exit_code=1)

    result = dispatch_with_retry({"id": 1}, dispatch=fake_dispatch, max_redispatches=2)
    assert result.status == "expired"
    assert calls == 3  # 1 initial + 2 redispatches — matches prior behavior


# ---------------------------------------------------------------------------
# scope_violation integration: dispatch_with_retry must not retry it
# ---------------------------------------------------------------------------


def test_dispatch_with_retry_does_not_retry_scope_violation() -> None:
    """scope_violation must stop after one attempt, even with budget remaining."""
    calls = 0

    def fake_dispatch(task, *, handoff=None):  # noqa: ANN001, ANN202, ARG001
        nonlocal calls
        calls += 1
        return FakeResult(status="scope_violation", exit_code=1)

    result = dispatch_with_retry({"id": 1}, dispatch=fake_dispatch, max_redispatches=5)
    assert result.status == "scope_violation"
    assert calls == 1  # no retry for terminal status


# ---------------------------------------------------------------------------
# Dispatcher-level: FleetDispatcher.dispatch() must not retry scope_violation
# ---------------------------------------------------------------------------


def test_fleet_dispatcher_does_not_retry_scope_violation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_fleet.config import load_fleet_config
    from agent_fleet.dispatcher import FleetDispatcher

    call_count = 0

    def fake_run_one(self, task, *, handoff=None):  # noqa: ANN001, ANN202, ARG001
        nonlocal call_count
        call_count += 1
        return FakeResult(status="scope_violation", exit_code=1)

    monkeypatch.setattr(FleetDispatcher, "_run_one", fake_run_one)

    fc = load_fleet_config(ROOT / "fleet.example.yaml")
    fc.max_redispatches = 3
    dispatcher = FleetDispatcher(config=fc)

    results = dispatcher.dispatch(
        goal="test scope violation",
        context="",
        persona="coder",
        workspace=str(ROOT),
        pipeline="simple",
    )
    assert len(results) == 1
    assert results[0].status == "scope_violation"
    assert call_count == 1  # terminal: no retry despite budget=3
