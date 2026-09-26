"""spec-1: the exported ``read_decisions`` must not report resolved decisions.

``read_decisions`` is the public reader for the human queue file and documents
itself as "Every queued decision, oldest first". The queue is append-only, so the
file interleaves raise records with resolution records (a human answers a fence
by appending ``{"item_id": ..., "resolved": true, ...}``). ``read_decisions``
does not filter those, and ``Decision.from_dict`` happily builds a Decision from
a resolution line because the line carries an ``item_id`` — producing a phantom
row with an empty reason, ``reason_class='unknown'`` and ``raised_epoch=0.0``.

``EscalationRouter.pending`` in the same module does filter them, and its
docstring names the exact harm:

    A human who resolved a fence, watched the lane come back, watched it fence
    again, and then checked the queue would be told their answer had already
    been given.

A consumer built on the exported reader gets the same result.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.escalate import EscalationRouter, read_decisions

if TYPE_CHECKING:
    from pathlib import Path

FENCE_REASON = "owner decision: do not edit the lockfile until the API is settled"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


def _queue(tmp_path: Path) -> tuple[Path, EscalationRouter]:
    """item-1 raised then resolved, item-2 still waiting for a human."""
    clock = FakeClock()
    router = EscalationRouter("op", clock=clock, decisions_file=tmp_path / "decisions.jsonl")
    router.route("item-1", FENCE_REASON)
    assert router.resolve("item-1", "proceed, the API is frozen this week") is True
    router.route("item-2", FENCE_REASON)
    return router.decisions_path, router


def test_read_decisions_reports_only_open_decisions(tmp_path: Path) -> None:
    """The correct outcome: one waiting item, not three rows."""
    path, router = _queue(tmp_path)
    assert [d.item_id for d in router.pending()] == ["item-2"], "precondition: one open item"

    decided = read_decisions(path)
    assert [d.item_id for d in decided] == ["item-2"], (
        f"read_decisions returned {[d.item_id for d in decided]}: the queue file holds "
        f"an item-1 raise, an item-1 resolution and an item-2 raise, and the exported "
        f"reader does not filter the resolution the way pending() does"
    )


def test_read_decisions_does_not_fabricate_a_decision_from_a_resolution_record(
    tmp_path: Path,
) -> None:
    """A resolution line is not a decision and must not become an empty one."""
    path, _router = _queue(tmp_path)
    rows = read_decisions(path)

    phantom = [d for d in rows if d.item_id == "item-1" and d.reason == ""]
    assert phantom == [], (
        f"read_decisions fabricated {phantom} from a resolution record "
        f"(reason='', reason_class={phantom[0].reason_class!r}, "
        f"raised_epoch={phantom[0].raised_epoch})" if phantom else ""
    )
    assert all(d.reason for d in rows), "a decision with no reason is not a decision"


def test_read_decisions_agrees_with_pending(tmp_path: Path) -> None:
    """The two readers of one file must not disagree.

    ``pending`` re-reads resolutions from disk on every call; the exported reader
    does not, so the divergence is visible within a single process.
    """
    path, router = _queue(tmp_path)
    assert [d.to_dict() for d in read_decisions(path)] == [d.to_dict() for d in router.pending()], (
        "read_decisions and pending() disagree about the same file: pending() "
        "applies resolutions, read_decisions does not"
    )


def test_a_resolved_decision_is_not_listed_as_still_waiting(tmp_path: Path) -> None:
    """The harm the docstring describes, stated as an assertion."""
    path, _router = _queue(tmp_path)
    waiting = {d.item_id for d in read_decisions(path)}
    assert "item-1" not in waiting, (
        "item-1 was resolved by a human but read_decisions still lists it as "
        "waiting, which is how a human who already gave an answer is told it "
        "was ignored"
    )
    # The resolution really is on disk, so this is a reader bug, not a data one.
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert any(line.get("resolved") for line in lines)
