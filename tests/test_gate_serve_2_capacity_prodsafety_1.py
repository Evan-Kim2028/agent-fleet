"""prodsafety-1: ``completed_this_tick`` returns a cumulative total, not a delta.

The docstring is unambiguous: "How many items reached a terminal stage *since
the last tick*." The implementation applies only an upper bound
(``t.epoch <= the_now``) and no lower one, so the return value is the number of
terminal transitions the board has ever recorded — monotonically non-decreasing,
and never returning to 0 once anything has finished.

A correct per-tick count returns 1 on the tick that merged and 0 on every tick
afterwards. With several items merging one per tick, it returns 1 per tick; this
implementation returns the running total (1, then 2, then 3).

The downstream consumer is the starvation guard, which resets solely on
``completed > 0``. A board that has *ever* merged therefore pins
``controller.idle_ticks`` at 0 permanently, so
``test_starvation_guard_collapses_lanes_and_prioritises_gates`` can never pass
when driven from a real board rather than from a hand-passed ``completed=0``.

To be explicit about blast radius: the supervisor branch that lands later
reimplements this as a windowed ``_completions_since_last_tick``, so today this
is a trap in a public API rather than a live outage. That is a reason to fix the
semantics, not a reason to leave a method whose contract is the opposite of its
behaviour.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.items import STAGE_ESCALATED, STAGE_MERGED, ItemBoard

if TYPE_CHECKING:
    from pathlib import Path

TICK_SECONDS = 10.0
ONE_HOUR = 3600.0


def test_a_completion_an_hour_old_is_not_counted_as_this_ticks_work(tmp_path: Path) -> None:
    clock = FakeClock(start_time=1_000_000.0)
    board = ItemBoard(tmp_path / "items.jsonl", clock=clock)
    board.record("repo-a#1", STAGE_MERGED)
    merged_at = clock.time()

    clock.advance(ONE_HOUR)

    counted = board.completed_this_tick(now=clock.time())
    assert counted == 0, (
        f"completed_this_tick() returned {counted} for a merge that happened at "
        f"epoch {merged_at:.0f}, an hour before the tick now={clock.time():.0f}; "
        f"no item reached a terminal stage during this tick"
    )


def test_three_items_over_three_ticks_count_one_per_tick_not_three(tmp_path: Path) -> None:
    clock = FakeClock(start_time=1_000_000.0)
    board = ItemBoard(tmp_path / "items.jsonl", clock=clock)

    per_tick: list[int] = []
    for index in range(3):
        board.record(f"repo-a#{index}", STAGE_MERGED)
        per_tick.append(board.completed_this_tick(now=clock.time()))
        clock.advance(TICK_SECONDS)

    assert per_tick == [1, 1, 1], (
        f"one item reached a terminal stage on each of three consecutive ticks, "
        f"so the per-tick signal should be [1, 1, 1]; got {per_tick} — the "
        f"method is summing the whole log rather than the tick window"
    )


def test_escalations_are_counted_but_still_only_within_the_window(tmp_path: Path) -> None:
    """Terminal-but-not-merged counts; an hour-old one still does not."""
    clock = FakeClock(start_time=1_000_000.0)
    board = ItemBoard(tmp_path / "items.jsonl", clock=clock)
    board.record("repo-b#1", STAGE_ESCALATED)
    assert board.completed_this_tick(now=clock.time()) == 1

    clock.advance(ONE_HOUR)
    assert board.completed_this_tick(now=clock.time()) == 0
