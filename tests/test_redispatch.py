"""Tests for hard-failure detection, handoff extraction, and the retry loop."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from agent_fleet.contracts.handoff import HandoffNote
from agent_fleet.redispatch import (
    _extract_handoff,
    _is_hard_failure,
    dispatch_with_retry,
)


@dataclass
class FakeResult:
    status: str
    files_modified: tuple[str, ...] = ()
    stderr: str = ""
    exit_code: int = 0


@pytest.mark.parametrize(
    "status, exit_code, expected",
    [
        ("error", 1, True),
        ("cancelled", 1, True),
        ("expired", 1, True),
        ("timeout", 1, True),
        ("scope_violation", 1, True),
        ("pipeline_nonzero", 2, True),
        ("verify_failed", 0, False),
        ("review_rejected", 0, False),
        ("success", 0, False),
    ],
)
def test_is_hard_failure_table(
    status: str, exit_code: int, expected: bool
) -> None:
    r = FakeResult(status=status, exit_code=exit_code)
    assert _is_hard_failure(r) is expected


def test_extract_handoff_captures_failure_context() -> None:
    r = FakeResult(
        status="expired",
        files_modified=("src/a.py", "src/b.py"),
        stderr="Cursor send status: expired",
    )
    note = _extract_handoff(r, previous=None)
    assert isinstance(note, HandoffNote)
    assert "expired" in note.failure_mode
    assert "src/a.py" in note.files_touched
    assert note.attempt_number == 1


def test_extract_handoff_chains_attempts() -> None:
    first = _extract_handoff(
        FakeResult(status="error", stderr="x"), previous=None
    )
    second = _extract_handoff(
        FakeResult(status="error", stderr="y"), previous=first
    )
    assert second.attempt_number == 2


def test_dispatch_with_retry_succeeds_first_try() -> None:
    calls = []

    def fake_dispatch(task, *, handoff=None):  # noqa: ANN001, ANN202, ARG001
        calls.append(handoff)
        return FakeResult(status="success")

    result = dispatch_with_retry({"id": 1}, dispatch=fake_dispatch, max_redispatches=1)
    assert result.status == "success"
    assert calls == [None]


def test_dispatch_with_retry_redispatches_on_hard_failure() -> None:
    statuses = iter(["expired", "success"])
    handoffs_seen = []

    def fake_dispatch(task, *, handoff=None):  # noqa: ANN001, ANN202, ARG001
        handoffs_seen.append(handoff)
        return FakeResult(status=next(statuses))

    result = dispatch_with_retry({"id": 1}, dispatch=fake_dispatch, max_redispatches=1)
    assert result.status == "success"
    assert handoffs_seen[0] is None
    assert handoffs_seen[1] is not None
    assert handoffs_seen[1].attempt_number == 1


def test_dispatch_with_retry_does_not_redispatch_soft_failure() -> None:
    calls = 0

    def fake_dispatch(task, *, handoff=None):  # noqa: ANN001, ANN202, ARG001
        nonlocal calls
        calls += 1
        return FakeResult(status="verify_failed")

    result = dispatch_with_retry({"id": 1}, dispatch=fake_dispatch, max_redispatches=3)
    assert result.status == "verify_failed"
    assert calls == 1  # only the initial attempt


def test_dispatch_with_retry_respects_budget() -> None:
    calls = 0

    def fake_dispatch(task, *, handoff=None):  # noqa: ANN001, ANN202, ARG001
        nonlocal calls
        calls += 1
        return FakeResult(status="expired")

    result = dispatch_with_retry({"id": 1}, dispatch=fake_dispatch, max_redispatches=2)
    assert result.status == "expired"
    assert calls == 3  # 1 initial + 2 redispatches
