"""contract-3: a throttle-abandoned queue records the outcome it reports.

Claim under test
----------------
``run_dispatch``'s throttle-abandon branch only did ``summary.errors += waiting``
and broke, so the lanes it never dispatched were persisted as
``{state: queued, reason: None}``.  ``THROTTLE_ABANDONED`` was dead code in
``ERROR_REASONS`` and ``render_summary`` printed the abandonment as plain queued
lanes with no reason, which is what docs/FLEET-OPS.md promises it will not do:
"records the lanes it never ran as ``throttle_abandoned`` (counted as errors, so
``exit_code()`` is 1)".

The reason must be recorded without making the lane terminal: a throttle is a
delay, so the next run of the same queue must still be able to dispatch it.  That
is the second assertion here, because recording the reason by flipping the state
to ``done`` would fix the first assertion and reintroduce the silent-drop bug
``DISPATCH_TERMINAL`` exists to prevent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet.fleet_ops import pressure
from agent_fleet.fleet_ops.dispatch import (
    DISPATCH_QUEUED,
    DISPATCH_TERMINAL,
    THROTTLE_ABANDONED,
    run_dispatch,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

#: A saturated box: CPU pressure far above the default ceiling.
SATURATED = pressure.Throttle(
    some_avg10=pressure.DEFAULT_PSI_AVG10_MAX * 4.0,
    path=Path("/fake/cpu.pressure"),
    available=True,
)


class _NeverSpawned:
    """A spawn that must never be reached: a saturated box launches nothing."""

    def __call__(self, argv: Sequence[str], **_kwargs: Any) -> Any:  # noqa: ANN401
        raise AssertionError(f"a saturated box must not launch anything, got {list(argv)}")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


def _run(tmp_path: Path) -> Any:  # noqa: ANN401
    repo = tmp_path / "repo"
    repo.mkdir()
    queue = tmp_path / "q.jsonl"
    queue.write_text(
        json.dumps({"lane": "lane-0", "repo": "acme", "task": "t0"}),
        encoding="utf-8",
    )
    return run_dispatch(
        operator="documents-0e",
        queue_path=queue,
        repos={"acme": str(repo)},
        max_lanes=2,
        max_gates=1,
        spawn=_NeverSpawned(),
        psi_reader=lambda: SATURATED,
        sleep=lambda _seconds: None,
        # The lowest bound that still reaches the abandonment branch: a run that
        # gives up after its first idle tick. Zero is refused outright, since a
        # dispatcher that may not wait at all is not a dispatcher.
        throttle_max_ticks=1,
        run_dir=tmp_path / "out",
    )


def test_a_saturated_box_records_throttle_abandoned_on_the_lanes_it_never_ran(
    tmp_path: Path,
) -> None:
    """The documented outcome is recorded, not just counted in a bare integer."""
    summary = _run(tmp_path)
    state = summary.state
    assert state is not None

    abandoned = sorted(
        lane.lane for lane in state.lanes.values() if lane.reason == THROTTLE_ABANDONED
    )

    assert abandoned == ["lane-0"], (
        f"expected lane-0 to be recorded as {THROTTLE_ABANDONED!r}, got {abandoned} "
        f"(summary={summary.to_dict()}); the constant is in ERROR_REASONS precisely so "
        "the outcome is recorded on the lane, and without it render_summary shows the "
        "abandonment as an unexplained queued lane"
    )
    assert summary.errors == 1, f"summary: {summary.to_dict()}"
    assert summary.exit_code() == 1, f"a dropped queue reported exit_code={summary.exit_code()}"


def test_a_recorded_abandonment_is_still_dispatchable_on_the_next_run(tmp_path: Path) -> None:
    """A throttle is a delay, so recording it must not make the lane terminal.

    Recording the reason by finishing the lane would satisfy the reason
    assertion and silently drop the queue: the next run reloads the record as
    terminal and never dispatches it again.
    """
    summary = _run(tmp_path)
    state = summary.state
    assert state is not None

    lane = state.lanes["lane-0"]

    assert lane.state == DISPATCH_QUEUED, (
        f"lane-0 is {lane.state!r} after a throttle; a throttle is a delay, not a "
        "verdict, so the next run must still be able to dispatch it"
    )
    assert lane.state not in DISPATCH_TERMINAL, (
        f"lane-0 became terminal ({lane.state!r}); the next run reloads it as finished "
        "and the queue is silently dropped"
    )
