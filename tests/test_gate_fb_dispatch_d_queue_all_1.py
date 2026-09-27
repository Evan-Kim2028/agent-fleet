"""all-1: the idle abandon is a delay, so the tail must stay re-dispatchable.

Claim under test
----------------
The idle (non-throttled) arm of ``run_dispatch``'s no-progress branch trips the
``idle_ticks > throttle_max_ticks`` bound and ``break``s without finishing any
lane and without counting an error, so a ten-item queue behind eight live
children is reported as ``exit_code() == 0`` -- on the theory that this is "the
silent-drop failure this module exists to prevent".

What the claim gets right is the observation: on the default settings
(``max_lanes=8``, ``tick_seconds=20.0``, ``throttle_max_ticks=3``) a queue larger
than the pool with children that take more than ~60s does stop after four
no-progress ticks, and ``summary.errors`` is 0.

What the claim gets wrong is the conclusion, and the module is explicit about
it. The bound is documented as *a wait and never a verdict*
(docs/FLEET-OPS.md, "The bound is a wait, never a verdict. It only abandons lanes
when nothing at all is in flight ... The same bound applies on an idle box, so a
child that never exits cannot hang the run either"), and the in-code comment at
the break says the same: "no lane is finished here, so the next run re-attaches
and collects whatever verdict that child owes." The lanes are *not* dropped --
they keep ``DISPATCH_QUEUED``/``DISPATCH_RUNNING``, which are non-terminal, so
the durable state re-dispatches them. A green exit is therefore accurate: the
run collected every verdict it was owed, and the outstanding work is recorded,
not discarded.

The two ways this could still be a real silent-drop are what these tests pin,
and neither is the exit code:

1. a terminal state written to a lane the run never launched -- unrecoverable,
   because the next run resumes only the lanes that ran; and
2. the abandoned tail being unreachable on the next run, which is what
   ``test_the_abandoned_tail_drains_on_the_next_run`` drives end to end.

The error accounting belongs to the *throttled* arm, which has no children in
flight to wait on: there the queue genuinely never ran, and
``throttle_abandoned`` is recorded and counted. This run has eight live
children; counting it as an error would report a run that launched eight of ten
lanes and is waiting on them as a failure, which is the same class of lie in the
other direction.
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
    DispatchState,
    load_state,
    run_dispatch,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

#: A healthy box: 1.0% CPU pressure against the 25.0% ceiling -- not throttled.
IDLE = pressure.Throttle(some_avg10=1.0, path=Path("/fake/cpu.pressure"), available=True)

LANES = 10
MAX_LANES = 8


class _LiveProc:
    """A lane child that never exits inside this run: ``poll()`` returns None.

    This is the "real coding agent" of the claim -- a lane that takes longer
    than ``throttle_max_ticks * tick_seconds``. It is exactly the child whose
    eventual exit frees the slot, and the reason the run waits rather than
    abandons.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode = None

    def poll(self) -> int | None:
        return None


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


class _Pool:
    """Ten queued lanes, eight live children: the claim's exact shape."""

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

    def run(self, **kwargs: Any) -> Any:  # noqa: ANN401
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
            **kwargs,
        )

    def _sleep(self, _seconds: float) -> None:
        self.sleeps += 1
        if self.sleeps > 16:
            raise AssertionError("the wait is unbounded")


def _states(summary: Any, state: str) -> list[str]:  # noqa: ANN401
    assert summary.state is not None
    return sorted(lane.lane for lane in summary.state.lanes.values() if lane.state == state)


def test_a_wait_on_live_children_is_not_counted_as_a_dropped_queue(tmp_path: Path) -> None:
    """A run that launched 8 of 10 lanes and is waiting on them is not a failure.

    The claim is that the idle arm reporting ``errors == 0`` is a silent drop.
    It is the opposite: the abandonment accounting (``THROTTLE_ABANDONED``,
    ``summary.errors += waiting``) is for the *throttled* arm, where nothing is
    in flight and the queue genuinely never ran. Here eight children are live
    and holding their slots. Counting that as an error would report a run that
    did real work and is waiting on it as a failure.
    """
    pool = _Pool(tmp_path)

    summary = pool.run()

    assert len(pool.spawned) == MAX_LANES, (
        f"expected the run to fill the pool with {MAX_LANES} children, spawned {len(pool.spawned)}"
    )
    abandoned = sorted(
        lane.lane for lane in summary.state.lanes.values() if lane.reason == THROTTLE_ABANDONED
    )
    assert not abandoned, (
        f"lanes {abandoned} were finished as {THROTTLE_ABANDONED!r} on an idle box "
        f"(psi throttled={pressure.throttled(IDLE)!r}); nothing here was held back by "
        f"the machine -- {MAX_LANES} live children were holding the slots"
    )
    assert summary.errors == 0, f"summary: {summary.to_dict()}"


def test_the_wait_is_bounded_so_a_child_that_never_exits_cannot_hang_the_run(
    tmp_path: Path,
) -> None:
    """The bound is real, and it is the bound the docs specify.

    ``throttle_max_ticks`` is what stops a wedged child from hanging the run
    forever. This is the *same* bound on an idle box as on a throttled one, so
    the run must stop after it -- and must not have finished anything on the way
    out.
    """
    pool = _Pool(tmp_path)

    pool.run()

    assert 0 < pool.sleeps <= 4, (
        f"the run slept {pool.sleeps} times; the bound is throttle_max_ticks=3 "
        "ticks of no progress, so it must stop at 4 (default tick_seconds=20.0, "
        "i.e. ~80s of wall time) and never hang"
    )


def test_the_abandoned_tail_is_left_resumable_not_given_a_verdict(tmp_path: Path) -> None:
    """The two lanes behind the pool keep their state and carry no verdict.

    This is the property that makes exit 0 honest. A terminal state here would
    be unrecoverable -- the next run resumes only the lanes that ran -- so it is
    the one thing an abandon path must never do, and the reason the bound is
    documented as "a wait, never a verdict".
    """
    pool = _Pool(tmp_path)

    summary = pool.run()

    tail = [f"lane-{n:02d}" for n in range(MAX_LANES, LANES)]
    consumed = sorted(
        lane.lane
        for lane in summary.state.lanes.values()
        if lane.lane in tail and (lane.state in DISPATCH_TERMINAL or lane.reason is not None)
    )
    assert not consumed, (
        f"lanes {consumed} behind the pool's high-water mark were given a verdict by a "
        f"run that launched only {len(pool.spawned)} of {LANES}: {summary.to_dict()}. "
        "A terminal state here is unrecoverable -- the next run resumes only the "
        "lanes that ran."
    )
    assert _states(summary, DISPATCH_QUEUED) == tail, (
        f"expected the un-run tail {tail} to stay QUEUED and re-dispatchable, got "
        f"{_states(summary, DISPATCH_QUEUED)}; summary={summary.to_dict()}"
    )


def test_the_abandoned_tail_drains_on_the_next_run(tmp_path: Path) -> None:
    """The end-to-end proof that nothing was dropped: run it again, it finishes.

    The claim's worry is that a green exit leaves work stranded. It does not.
    Once the children this run was waiting on are gone, the durable state makes
    their slots available again, the queued tail is dispatched, and every lane
    reaches a real terminal verdict. That is the documented contract -- "a
    throttle is a delay, so the next run resumes the queue" -- and it is what
    distinguishes a delay from the silent drop.
    """
    pool = _Pool(tmp_path)
    first = pool.run()
    assert first.exit_code() == 0, f"summary: {first.to_dict()}"

    # The children this run was waiting on finish; the next run picks the queue
    # back up and drives the tail to a verdict. A child that exits immediately
    # on its first poll models a machine whose agents have all finished.
    class _FinishedProc:
        def __init__(self, pid: int) -> None:
            self.pid = pid
            self.returncode = None

        def poll(self) -> int | None:
            return 0

    pid = 9000

    def spawn(*_args: Any, **_kwargs: Any) -> Any:  # noqa: ANN401
        nonlocal pid
        pid += 1
        return _FinishedProc(pid)

    reloaded: DispatchState = load_state("documents-0e", queue_path=str(pool.queue))

    second = run_dispatch(
        operator="documents-0e",
        queue_path=pool.queue,
        repos={"acme": str(pool.repo)},
        max_lanes=MAX_LANES,
        max_gates=4,
        state=reloaded,
        spawn=spawn,
        psi_reader=lambda: IDLE,
        sleep=lambda _s: None,
        run_dir=pool.repo / "out",
    )
    assert second.state is not None

    terminal = sorted(
        lane.lane for lane in second.state.lanes.values() if lane.state in DISPATCH_TERMINAL
    )
    assert terminal == sorted(f"lane-{n:02d}" for n in range(LANES)), (
        f"the queue did not drain: {len(terminal)}/{LANES} lanes terminal after a "
        f"second run over the same durable state. If the first run had dropped the "
        f"tail these could never reach a verdict. run 1={first.to_dict()} "
        f"run 2={second.to_dict()}"
    )
