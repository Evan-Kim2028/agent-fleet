"""spec-3: the infra retry budget must survive a serve restart.

The module docstring states the contract:

    ``infra``
        Something in the environment failed and the work itself is fine ...
        **Retried once automatically.** Not more: an infra failure that survives
        one retry is an infra failure that needs a human.

and the on-disk queue is described as append-only precisely so it survives:

    A human resolves a decision by appending a resolution record, so the queue
    is a log rather than a mutable list.

But the counter enforcing the contract, ``EscalationRouter.attempts``, is
memory-only — created in ``__init__``, never persisted, never re-read. A serve
restart constructs a fresh router over the same queue file and hands the same
still-failing item a fresh budget, so it gets its automatic retry again.

The same PR already applies the correct argument one module over.
``supervisor.py`` persists crash history for exactly this reason:

    The crash history is persisted, so a supervisor that restarts does not hand
    a crash-looping component a fresh budget.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.escalate import (
    ACTION_DECIDE,
    ACTION_RETRY,
    MAX_INFRA_RETRIES,
    EscalationRouter,
)

if TYPE_CHECKING:
    from pathlib import Path

INFRA_REASON = "could not run tests: git fetch timed out"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


def _router(tmp_path: Path, clock: FakeClock) -> EscalationRouter:
    return EscalationRouter("op", clock=clock, decisions_file=tmp_path / "decisions.jsonl")


def test_the_infra_retry_budget_survives_a_serve_restart(tmp_path: Path) -> None:
    """The correct outcome: exactly one automatic retry, ever.

    A fresh ``EscalationRouter`` over the same on-disk queue stands in for a
    serve restart — the same object the supervisor builds on boot, over the same
    file.
    """
    clock = FakeClock()

    router = _router(tmp_path, clock)
    first = router.route("item-1", INFRA_REASON)
    assert first.action == ACTION_RETRY, "precondition: the first attempt retries"

    # --- serve restarts: a brand new router over the same queue file.
    after_restart = _router(tmp_path, clock)
    second = after_restart.route("item-1", INFRA_REASON)
    third = after_restart.route("item-1", INFRA_REASON)

    expected = [ACTION_RETRY, ACTION_DECIDE, ACTION_DECIDE]
    actual = [first.action, second.action, third.action]
    assert actual == expected, (
        f"actions {actual}: `attempts` is memory-only, so a restarted serve hands the "
        f"same still-failing item a fresh budget and it received more than the "
        f"contracted {MAX_INFRA_RETRIES} automatic retry"
    )
    assert second.queued is True, "the second attempt must reach a human"


def test_an_infra_item_is_never_escalated_however_often_serve_restarts(tmp_path: Path) -> None:
    """The unbounded half: with a fresh router each time, ``decide`` is unreachable.

    Every restart re-reads the queue file, so the item's escalation history is
    right there on disk — but nothing reads it, so the item is retried forever and
    never reaches a human.
    """
    clock = FakeClock()
    actions = [_router(tmp_path, clock).route("item-1", INFRA_REASON).action for _ in range(5)]
    assert ACTION_DECIDE in actions, (
        f"actions {actions} over five serve restarts: the item never reached a "
        f"human, because each fresh EscalationRouter starts its retry budget at zero"
    )


def test_a_fresh_router_reconstructs_its_budget_from_the_queue_file(tmp_path: Path) -> None:
    """The on-disk state a restarted router must honour.

    ``pending`` already re-derives open decisions from the file on every call, so
    a human's resolution is honoured immediately after a restart. The retry
    counter has no such path.
    """
    clock = FakeClock()
    router = _router(tmp_path, clock)
    router.route("item-1", INFRA_REASON)

    after_restart = _router(tmp_path, clock)
    assert after_restart.attempts_for("item-1") == 1, (
        "a restarted serve has no idea item-1 has already had its one automatic "
        "retry, even though the queue file recording it is open in front of it"
    )
