"""correctness_3: a throttled queue must not be reported as a successful run.

Claim under test
----------------
With CPU PSI above the ceiling, ``plan_tick`` returns ``[]``, and ``run_dispatch``
reaches its ``if not actions`` branch. Neither of the two questions it asks there
knows about the throttle:

* ``blocked_lanes()`` returns ``[]`` -- the lanes have no ``depends_on``, so
  nothing is stuck on a dependency;
* ``_complete()`` is ``not any(lane.state in LANE_SLOT_STATES ...)``, and
  ``DISPATCH_QUEUED`` is deliberately not a slot state, so a queue of five
  untouched lanes reads as "complete".

The loop therefore breaks on the first tick. The run returns
``{lanes: 5, launched: 0, ...}``, ``exit_code()`` is 0, all five lanes are still
``queued``, and the injected sleep is never called -- a green exit code for a
queue that was silently dropped.

The expected behaviour is stated in the module's own docstring: the PSI
throttle exists because a false block must not stop dispatch, and a throttle is
a *delay*. A saturated box has to keep re-evaluating until the queue drains.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet.fleet_ops import pressure
from agent_fleet.fleet_ops.dispatch import DISPATCH_QUEUED, run_dispatch

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Above the ceiling below, so every tick is throttled.
THROTTLED = pressure.Throttle(some_avg10=42.0, path=Path("/fake/cpu.pressure"), available=True)
PSI_CEILING = 25.0
QUEUE_SIZE = 5


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


class _NeverSpawned(Exception):
    """Raised if a launch is attempted under a throttle that never lifts."""


def test_a_persistently_throttled_run_reports_the_queue_it_dropped(tmp_path: Path) -> None:
    """A run that launched nothing must not exit 0.

    The throttle here never lifts, so the only correct outcomes are "keep
    waiting" or "report the lanes as undispatched". Reporting
    ``exit_code() == 0`` for five untouched lanes is neither.
    """
    summary, sleeps, states = _run_never_spawned(tmp_path)

    assert summary.launched == 0, "sanity: the throttle suppressed every launch"
    assert not sleeps, "sanity: the run ended on its first tick"

    untouched = sorted(name for name, state in states.items() if state == DISPATCH_QUEUED)
    assert not untouched, (
        f"lanes {untouched} were left {DISPATCH_QUEUED!r} by a finished run; "
        f"they were dropped, not dispatched (summary={summary.to_dict()})"
    )


def test_a_persistently_throttled_run_is_not_reported_as_success(tmp_path: Path) -> None:
    """``exit_code()`` must not be 0 for a queue that never ran.

    ``DispatchSummary.exit_code`` is 0 when nothing escalated and nothing
    errored. Dropping five lanes is neither, so a green exit here is a silent
    data-loss signal to whatever drives ``fleet dispatch``.
    """
    summary, _sleeps, _states = _run_never_spawned(tmp_path)

    assert summary.exit_code() != 0, (
        f"a queue of {QUEUE_SIZE} lanes was never dispatched but exit_code() "
        f"reported success (summary={summary.to_dict()})"
    )


def test_a_throttled_run_keeps_re_evaluating_instead_of_finishing(tmp_path: Path) -> None:
    """Under a throttle with no work to do, the loop must tick, not exit.

    ``sleep`` is the dispatcher's wait primitive: it is what separates "the
    machine is busy, try again in a tick" from "there is nothing left to do".
    A run that never calls it made no attempt to wait.
    """
    _summary, sleeps, _states = _run_never_spawned(tmp_path)

    assert sleeps, (
        "the dispatcher returned from a saturated queue without ever waiting; "
        "throttling is a delay, not a completion"
    )


def _run_never_spawned(
    tmp_path: Path,
) -> tuple[Any, list[float], dict[str, str]]:
    """Drive ``run_dispatch`` with a throttle that never lifts."""
    repo = tmp_path / "repo"
    repo.mkdir()
    queue = tmp_path / "q.jsonl"
    queue.write_text(
        "\n".join(
            json.dumps({"lane": f"C{n}-fix", "repo": "acme", "task": f"task {n}"})
            for n in range(QUEUE_SIZE)
        ),
        encoding="utf-8",
    )
    sleeps: list[float] = []

    def spawn(argv: Sequence[str], **kwargs: Any) -> Any:  # noqa: ANN401, ARG001
        raise _NeverSpawned(argv)

    summary = run_dispatch(
        operator="documents-0e",
        queue_path=queue,
        repos={"acme": str(repo)},
        max_lanes=8,
        max_gates=4,
        spawn=spawn,
        psi_reader=lambda: THROTTLED,
        sleep=sleeps.append,
        run_dir=tmp_path / "out",
        psi_avg10_max=PSI_CEILING,
    )
    states = {name: lane.state for name, lane in summary.state.lanes.items()}
    return summary, sleeps, states
