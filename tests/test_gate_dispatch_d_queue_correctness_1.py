"""correctness_1: an idle tick is not a throttle tick, and a dropped lane is not ``done``.

Claim under test
----------------
``run_dispatch``'s idle branch increments ``throttled_ticks`` in its ``else`` --
every state that is *not* "throttled with no queued work".  An idle tick where
lanes hold their slots but the queue still has work behind them is not a
throttled tick, yet it is counted.  On a healthy box a queue larger than
``--max-lanes`` therefore abandons its tail after four ticks, mid-flight.

``_finish`` then marks the abandoned lanes ``DISPATCH_DONE``/``throttle_abandoned``
while their eight siblings are still running.  ``DISPATCH_DONE`` is terminal, so
the next run of the same queue resumes only the survivors -- and reports
``0 errors``/exit 0 for the lanes it dropped, which is the silent-drop failure
the module docstring says it exists to prevent.  ``DEFAULT_MAX_THROTTLE_TICKS``
even promises that "re-running the dispatcher resumes the queue from where it
stopped"; for these lanes it cannot.

The test drives the real ``run_dispatch`` twice against the same durable state
with an always-idle PSI reader (1.0%, ceiling 25.0), 10 lanes and
``max_lanes=8``: 8 long-running lane children, 2 still queued.  The first run
must not mark a queued lane terminal on a healthy box; if it does, the second
run must not report the dropped lanes as a clean success.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet.fleet_ops import pressure
from agent_fleet.fleet_ops.dispatch import (
    DISPATCH_DONE,
    DISPATCH_FAILED,
    THROTTLE_ABANDONED,
    DispatchState,
    run_dispatch,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

#: A healthy box: 1.0% CPU pressure against a 25.0% ceiling.
IDLE = pressure.Throttle(some_avg10=1.0, path=Path("/fake/cpu.pressure"), available=True)

LANES = 10
MAX_LANES = 8


class _LiveProc:
    """A lane child that is still running: ``poll()`` returns None, forever."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode = None

    def poll(self) -> int | None:
        return None


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


class _Swarm:
    """``max_lanes`` long-running lane children and a queue that outlasts them."""

    def __init__(self, tmp_path: Path) -> None:
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        self.queue = tmp_path / "q.jsonl"
        self.queue.write_text(
            "\n".join(
                json.dumps({"lane": f"lane-{n:02d}", "repo": "acme", "task": f"t{n}"})
                for n in range(LANES)
            ),
            encoding="utf-8",
        )
        self.spawned: list[str] = []
        self.sleeps = 0
        self._pid = 3000

    def _spawn(self, argv: Sequence[str], **_kwargs: Any) -> Any:  # noqa: ANN401
        self.spawned.append(argv[4] if len(argv) > 4 else "")
        self._pid += 1
        return _LiveProc(self._pid)

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
            run_dir=Path(self.repo) / "out",
        )

    def _sleep(self, _seconds: float) -> None:
        self.sleeps += 1
        if self.sleeps > 16:
            raise AssertionError("run_dispatch is still ticking after 16 sleeps")


def _abandoned(state: DispatchState) -> list[str]:
    return sorted(
        lane.lane
        for lane in state.lanes.values()
        if lane.reason == THROTTLE_ABANDONED or lane.state in (DISPATCH_DONE, DISPATCH_FAILED)
    )


def test_a_healthy_box_does_not_abandon_the_tail_of_a_full_queue(tmp_path: Path) -> None:
    """Waiting for 8 running lanes is not CPU throttling.

    PSI reads 1% on every tick -- the machine is idle. Counting those ticks
    against ``DEFAULT_MAX_THROTTLE_TICKS`` finishes the two lanes still queued
    behind the running ones as ``throttle_abandoned`` and exits 1, which is the
    claimed defect.
    """
    swarm = _Swarm(tmp_path)

    summary = swarm.run()

    assert not _abandoned(summary.state), (
        f"a healthy box (psi throttled={pressure.throttled(IDLE)!r}, "
        f"some_avg10=1.0 vs ceiling {pressure.DEFAULT_PSI_AVG10_MAX}) finished "
        f"{_abandoned(summary.state)} as terminal on tick {swarm.sleeps} while "
        f"{MAX_LANES} sibling lanes were still running; summary={summary.to_dict()}"
    )
    assert summary.errors == 0, f"summary: {summary.to_dict()}"
    assert summary.exit_code() == 0, f"summary: {summary.to_dict()}"


def test_a_dropped_lane_is_not_reported_as_a_clean_success_on_the_rerun(tmp_path: Path) -> None:
    """A lane the dispatcher gave up on must never come back as a green re-run.

    ``_finish`` writes ``DISPATCH_DONE``, which is terminal: the next run of the
    same queue sees a finished lane, plans nothing for it, and reports 0 errors
    and exit 0 -- the two lanes it dropped are now indistinguishable from the
    eight that were gated.  Either the abandoned lane stays resumable, or the
    re-run must keep reporting it as an error.
    """
    swarm = _Swarm(tmp_path)
    first = swarm.run()
    dropped = _abandoned(first.state)

    second = swarm.run()

    reported = {lane.lane for lane in second.state.lanes.values()}
    still_bad = _abandoned(second.state)
    assert not dropped or still_bad, (
        f"run 1 dropped {dropped} (errors={first.errors}, exit={first.exit_code()}); "
        f"run 2 over the same queue then reported errors={second.errors} "
        f"exit={second.exit_code()} for lanes {sorted(reported)} -- the dropped work "
        "is now indistinguishable from a clean run, because DISPATCH_DONE is terminal"
    )
    assert second.exit_code() != 0 or not dropped, (
        f"run 1 dropped {dropped} but run 2 exits 0; a dropped queue is reported as a "
        "clean success, which is the silent-drop failure this module documents"
    )
    assert len(reported) == LANES, f"run 2 saw {len(reported)} lanes, expected {LANES}"
