"""correctness-3: queue_depth must not count escalated items as actionable work.

``status.py`` computes the fleet's queue depth as::

    "queue_depth": sum(depths.get(stage, 0) for stage in STAGES if stage not in ("merged",))

``items.py`` is explicit that ``escalated`` is terminal, not merely done:

    ``escalated`` is terminal but not dead: the routing in
    :mod:`agent_fleet.serve.escalate` may move an item back to ``queued`` ...

and ``TERMINAL_STAGES = frozenset({STAGE_MERGED, STAGE_ESCALATED})``.

So the exclusion list names one member of a two-member terminal set. A fleet
whose only outstanding work is parked awaiting a human decision reports
``queue_depth`` above zero, and ``check_no_progress`` — which returns early only
when ``queued_depth <= 0`` — proceeds to restart its components for work that
nobody can pick up, with a reason string claiming items are queued.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.config import ServeConfig
from agent_fleet.serve.items import (
    STAGE_ESCALATED,
    STAGE_MERGED,
    STAGE_QUEUED,
    TERMINAL_STAGES,
    ItemBoard,
)
from agent_fleet.serve.status import status_snapshot
from agent_fleet.serve.supervisor import Supervisor

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


def _board(tmp_path: Path, clock: FakeClock) -> ItemBoard:
    board = ItemBoard(tmp_path / "items.jsonl", clock=clock)
    for i in range(7):
        board.record(f"escalated-{i}", STAGE_ESCALATED, reason_class="fence")
    for i in range(5):
        board.record(f"merged-{i}", STAGE_MERGED)
    return board


def test_escalated_and_merged_are_both_terminal() -> None:
    """The data model both callers must agree on."""
    assert STAGE_ESCALATED in TERMINAL_STAGES
    assert STAGE_MERGED in TERMINAL_STAGES


def test_queue_depth_ignores_terminal_stages(tmp_path: Path) -> None:
    """The correct outcome: nothing runnable means depth zero.

    Twelve items sit at terminal stages, so there is no work waiting on a
    component, and the status screen must not tell the operator otherwise.
    """
    clock = FakeClock()
    board = _board(tmp_path, clock)
    sup = Supervisor("op", ServeConfig(operator="op"), clock=clock)

    snapshot = status_snapshot(
        "op", sup, board, capacity_file=tmp_path / "absent-capacity.json"
    )
    depth = snapshot["depth"]
    assert depth[STAGE_ESCALATED] == 7
    assert depth[STAGE_MERGED] == 5
    assert snapshot["queue_depth"] == 0, (
        f"queue_depth={snapshot['queue_depth']} for a board whose only items are "
        f"terminal ({depth}); the exclusion list names 'merged' but not 'escalated', "
        f"so a fleet parked on an undrained human decision queue reads as having "
        f"work waiting"
    )


def test_queue_depth_still_counts_real_work(tmp_path: Path) -> None:
    """A fleet with genuinely runnable work must still report it."""
    clock = FakeClock()
    board = ItemBoard(tmp_path / "items.jsonl", clock=clock)
    for i in range(4):
        board.record(f"queued-{i}", STAGE_QUEUED)
    for i in range(3):
        board.record(f"escalated-{i}", STAGE_ESCALATED, reason_class="fence")
    sup = Supervisor("op", ServeConfig(operator="op"), clock=clock)

    snapshot = status_snapshot(
        "op", sup, board, capacity_file=tmp_path / "absent-capacity.json"
    )
    assert snapshot["queue_depth"] == 4, (
        f"queue_depth={snapshot['queue_depth']}: the 4 queued items must be counted, "
        f"the 3 escalated ones must not"
    )
