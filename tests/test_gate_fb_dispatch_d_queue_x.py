"""all-1: a full pool with ready work behind it is a wait, not an early break.

Claim under test
----------------
``run_dispatch``'s idle branch used to ``break`` the moment the lane pool was
saturated on a *healthy* box::

    if not throttled:
        if _dispatchable_lanes(current, cluster_order=cluster_order):
            if not _can_launch_now(current, max_lanes=max_lanes):
                # Lanes are ready and every slot is held by a child that
                # is still running. Nothing but one of those children
                # exiting frees a slot, so waiting cannot help.
                break

The adjacent comment is factually wrong: a running child exiting is *precisely*
what frees the slot, and the same function waits correctly when the queue fits
entirely inside the pool. So on any queue larger than ``max_lanes`` -- which is
the default (``max_lanes=8``) against any real triage queue -- the branch that
should have waited instead broke on the first saturated tick, silently never
launching the tail behind the pool's high-water mark and still reporting exit 0.

The repro: six queued lanes, ``max_lanes=2``, an idle PSI reader
(``some_avg10=1.0`` against the 25.0 ceiling) and lane children that stay
alive. Tick 1 launches ``lane-0``/``lane-1`` and produces actions; tick 2 has
``lane-2``..``lane-5`` queued and dependency-free, both slots occupied, so
``plan_tick`` returns ``[]`` and the saturated branch fired.

Observed on the unfixed code: **0** calls to the injected ``sleep`` (the loop
never waited once), ``summary={'lanes': 6, 'launched': 2, 'errors': 0}``,
``exit_code() == 0``, and ``lane-2``..``lane-5`` left at ``state='queued'`` with
``reason=None``. Contrast the same queue with one lane and ``max_lanes=8``,
which sleeps and logs ``no lane progress for 4 ticks`` -- the branch worked only
when the whole queue fit inside the pool.

The defect is the *premature* break, so the assertions here pin the two things
the break destroyed: the loop must actually reach its (bounded) wait, and the
tail must stay resumable rather than being consumed by a verdict it never got.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet.fleet_ops import pressure
from agent_fleet.fleet_ops.dispatch import (
    DISPATCH_FAILED,
    DISPATCH_QUEUED,
    run_dispatch,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

#: A healthy box: 1.0% CPU pressure against the 25.0% ceiling -- not throttled.
IDLE = pressure.Throttle(some_avg10=1.0, path=Path("/fake/cpu.pressure"), available=True)

LANES = 6
MAX_LANES = 2


class _LiveProc:
    """A lane child that never exits: ``poll()`` returns None, forever.

    This is the child whose eventual exit frees the slot, so it is exactly the
    process the broken branch claimed nothing was waiting for.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode = None

    def poll(self) -> int | None:
        return None


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


class _SaturatedPool:
    """A queue that outlasts the pool, on a box that is not under any pressure."""

    def __init__(self, tmp_path: Path) -> None:
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        self.queue = tmp_path / "q.jsonl"
        self.queue.write_text(
            "\n".join(
                json.dumps({"lane": f"lane-{n}", "repo": "acme", "task": f"t{n}"})
                for n in range(LANES)
            ),
            encoding="utf-8",
        )
        self.spawned: list[str] = []
        self.sleeps: list[float] = []
        self._pid = 4000

    def _spawn(self, argv: Sequence[str], **_kwargs: Any) -> Any:  # noqa: ANN401
        self.spawned.append(argv[4] if len(argv) > 4 else "")
        self._pid += 1
        return _LiveProc(self._pid)

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if len(self.sleeps) > 16:
            raise AssertionError(
                f"still ticking after {len(self.sleeps)} sleeps; the wait is unbounded"
            )

    def run(self) -> Any:  # noqa: ANN401
        return run_dispatch(
            operator="documents-0e",
            queue_path=self.queue,
            repos={"acme": str(self.repo)},
            max_lanes=MAX_LANES,
            max_gates=4,
            spawn=self._spawn,
            psi_reader=lambda: IDLE,
            sleep=self._sleep,
            run_dir=self.repo / "out",
        )


def test_a_full_pool_with_ready_work_behind_it_is_waited_on_not_broken_out_of(
    tmp_path: Path,
) -> None:
    """The saturated branch must reach the sleep, not ``break`` past it.

    The unfixed code took the "waiting cannot help" branch on the first
    saturated tick and never slept at all. A run that filled the pool and then
    refused to wait left every lane behind the high-water mark queued forever.
    """
    swarm = _SaturatedPool(tmp_path)

    swarm.run()

    assert swarm.sleeps, (
        "the loop broke out of the dispatch cycle the moment the pool was full on a "
        f"healthy box (psi throttled={pressure.throttled(IDLE)!r}); it never called "
        "sleep even once, so every lane queued behind the pool's high-water mark was "
        "dropped without a verdict -- waiting on a running child is exactly what frees "
        f"a slot. queue={LANES} lanes, max_lanes={MAX_LANES}"
    )


def test_the_tail_of_a_queue_larger_than_the_pool_stays_resumable(tmp_path: Path) -> None:
    """Lanes the run never got to must not be consumed by a verdict.

    ``DISPATCH_DONE`` is terminal, so a lane finished without ever running can
    never be picked up by the next run of the same queue. The bounded wait
    therefore has to leave the un-run tail exactly as it found it: queued, and
    with no reason recorded.
    """
    swarm = _SaturatedPool(tmp_path)

    summary = swarm.run()

    tail = [f"lane-{n}" for n in range(MAX_LANES, LANES)]
    consumed = sorted(
        lane.lane
        for lane in summary.state.lanes.values()
        if lane.lane in tail and (lane.reason is not None or lane.state == DISPATCH_FAILED)
    )
    assert not consumed, (
        f"lanes {consumed} behind the pool's high-water mark were given a verdict by a "
        f"run that launched only {len(swarm.spawned)} of {LANES}: "
        f"summary={summary.to_dict()}. A terminal state here is unrecoverable -- the next "
        "run of this queue resumes only the lanes that ran."
    )
    still_queued = sorted(
        lane.lane
        for lane in summary.state.lanes.values()
        if lane.lane in tail and lane.state == DISPATCH_QUEUED
    )
    assert still_queued == tail, (
        f"expected the un-run tail {tail} to stay QUEUED and re-dispatchable, got "
        f"{still_queued}; summary={summary.to_dict()}"
    )
