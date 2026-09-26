"""contract_3: a CPU-throttled box must delay dispatch, not abandon the queue.

Claim under test
----------------
When ``psi_reader`` reports CPU pressure above ``psi_avg10_max``, ``plan_tick``
returns ``[]`` -- the launch loop is broken by the throttle. ``run_dispatch``
then takes its ``if not actions`` branch and asks two questions:
``blocked_lanes()`` and ``_complete()``. Neither of them knows anything about
the throttle:

* ``blocked_lanes()`` only reports lanes stuck on an unresolvable dependency;
  with no ``depends_on`` it returns ``[]``.
* ``_complete()`` is ``not any(lane.state in LANE_SLOT_STATES ...)``, and every
  lane is still ``DISPATCH_QUEUED`` -- a state that is deliberately *not* a slot
  state.

So ``_complete()`` is True, the loop breaks immediately, ``sleep`` is never
called, and the run reports success having launched nothing.

Throttling is a delay, not a cancellation. The documented behaviour (module
docstring, failure 5) is that pressure "fails open" and that the dispatcher
re-evaluates every ``--tick-seconds``; a saturated machine must keep waiting for
the queue to drain, and must never exit 0 having silently dropped every lane.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet.fleet_ops import pressure
from agent_fleet.fleet_ops.dispatch import (
    DispatchItem,
    DispatchState,
    LaunchLane,
    merge_queue,
    plan_tick,
    run_dispatch,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

#: 90% of the last 10s runnable-but-waiting: unmistakably saturated.
SATURATED = pressure.Throttle(some_avg10=90.0, path=Path("/fake/cpu.pressure"), available=True)
#: Same reader's idle reading, used as the control.
IDLE = pressure.Throttle(some_avg10=0.0, path=Path("/fake/cpu.pressure"), available=True)
PSI_CEILING = 25.0
LANES = ("alpha", "beta", "gamma")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


class _Run:
    """A throttled ``run_dispatch``, with the sleep that models the wait."""

    def __init__(self, tmp_path: Path, throttle: pressure.Throttle, lanes: int) -> None:
        self.lanes = lanes
        self.tmp_path = tmp_path
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        self.queue = tmp_path / "q.jsonl"
        self.queue.write_text(
            "\n".join(
                json.dumps({"lane": f"lane-{n}", "repo": "acme", "task": f"t{n}"})
                for n in range(lanes)
            ),
            encoding="utf-8",
        )
        self.spawned: list[list[str]] = []
        self.sleeps: list[float] = []
        self._psi = [throttle]
        self._pid = 2000

    def _read_psi(self) -> pressure.Throttle:
        return self._psi[0]

    def _spawn(self, argv: Sequence[str], **kwargs: Any) -> Any:  # noqa: ANN401
        argv = list(argv)
        self.spawned.append(argv)
        self._pid += 1
        with kwargs["stdout"] as log:
            log.write('{"state": "no_pr"}')
        return _Proc(self._pid, 0)

    def _sleep(self, seconds: float) -> None:
        # A real dispatcher re-reads pressure every tick; the pressure drops
        # after a few ticks, which is the whole point of throttling.
        self.sleeps.append(seconds)
        if len(self.sleeps) >= 2:
            self._psi[0] = pressure.Throttle(
                some_avg10=0.0, path=Path("/fake/cpu.pressure"), available=True
            )

    def run(self) -> Any:  # noqa: ANN401
        return run_dispatch(
            operator="documents-0e",
            queue_path=self.queue,
            repos={"acme": str(self.repo)},
            max_lanes=8,
            max_gates=4,
            spawn=self._spawn,
            psi_reader=self._read_psi,
            sleep=self._sleep,
            run_dir=self.tmp_path / "out",
            psi_avg10_max=PSI_CEILING,
        )


class _Proc:
    def __init__(self, pid: int, exit_code: int) -> None:
        self.pid = pid
        self.returncode = exit_code

    def poll(self) -> int:
        return self.returncode


def test_a_throttled_dispatcher_waits_instead_of_returning_a_green_exit(
    tmp_path: Path,
) -> None:
    """The run must not finish until the queue drains; it must not exit 0 empty.

    The observable is the pair: the loop has to keep ticking (``sleep`` called)
    and it has to launch the work once the pressure clears. A dispatcher that
    reports success while having launched nothing is the bug.
    """
    run = _Run(tmp_path, SATURATED, len(LANES))
    summary = run.run()

    assert summary.launched == len(LANES), (
        f"launched {summary.launched} of {len(LANES)} lanes; summary={summary.to_dict()}"
    )
    assert run.sleeps, "a saturated tick must delay before re-evaluating"
    assert summary.exit_code() == 0, f"summary: {summary.to_dict()}"


def test_a_throttled_dispatcher_never_reports_success_with_the_queue_intact(
    tmp_path: Path,
) -> None:
    """No lane may be left ``queued`` in a finished run.

    ``_complete()`` says "nothing occupies a slot", which a throttled queue
    satisfies without having done anything. If a run ends with lanes still
    ``queued``, they were dropped, not dispatched.
    """
    run = _Run(tmp_path, SATURATED, len(LANES))
    summary = run.run()

    still_queued = sorted(
        name for name, lane in summary.state.lanes.items() if lane.state == "queued"
    )
    assert not still_queued, (
        f"lanes {still_queued} were left in state 'queued' by a finished run "
        f"(summary={summary.to_dict()}); the queue was dropped, not dispatched"
    )


def test_throttling_still_suppresses_launches_in_a_single_tick() -> None:
    """Control: the throttle's correct half, pinned so a fix cannot undo it.

    ``plan_tick`` must not launch *any* lane on a saturated tick -- otherwise
    the fix for the two tests above could be "ignore the throttle", which
    reintroduces the load-200 incident the PSI throttle exists to prevent.
    """
    state = _queued_state(3)
    saturated_actions = plan_tick(
        state, max_lanes=8, max_gates=4, psi=SATURATED, psi_avg10_max=PSI_CEILING
    )
    assert not [a for a in saturated_actions if isinstance(a, LaunchLane)], (
        f"a saturated tick must launch nothing, got {saturated_actions}"
    )

    # The same state with pressure relieved does launch -- so the suppression
    # above is the throttle, not an empty queue.
    idle_actions = plan_tick(state, max_lanes=8, max_gates=4, psi=IDLE, psi_avg10_max=PSI_CEILING)
    assert [a for a in idle_actions if isinstance(a, LaunchLane)], (
        "an idle tick must launch the queued lanes"
    )


def _queued_state(count: int) -> Any:  # noqa: ANN401
    return merge_queue(
        DispatchState(operator="documents-0e"),
        [
            DispatchItem.from_dict({"lane": f"lane-{n}", "repo": "acme", "task": f"t{n}"})
            for n in range(count)
        ],
    )
