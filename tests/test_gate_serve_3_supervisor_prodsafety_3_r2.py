"""prodsafety-3: a restored restart_due can strand a component forever.

``ChildState.restart_due`` is documented (supervisor.py:170-173) as an epoch:
"When the backoff owed by the last exit is due", and ``_ensure_running`` gates
on ``state.restart_due > now`` where ``now = self.clock.time()`` -- wall clock
(supervisor.py:602, 613).

But the module's own rule, in the very docstring of
:class:`~agent_fleet.serve.clock.Clock`, is that the supervisor's backoff must
come from the *monotonic* clock so an NTP correction cannot shorten it, and
``_restore()`` loads ``restart_due`` straight out of the state file verbatim
(supervisor.py:272-279). Nothing rebases it onto the new clock, and nothing
anywhere clears it.

So whenever the wall clock the new supervisor reads is earlier than the one the
state file was written with -- an NTP step backwards, a host resuming with a
clock that started behind, a restored backup of the serve directory -- every
component that was mid-backoff has a deadline hours in the future, is skipped
by ``_ensure_running`` on every tick, and is never started again. The operator
sees ``backoff`` in ``serve status`` and no component.

The state below is what ``save()`` writes: a component in backoff with a
deadline 600s ahead of the clock the new supervisor is reading.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.config import ComponentSpec, ServeConfig, WatchdogConfig
from agent_fleet.serve.paths import state_path, write_json_atomic
from agent_fleet.serve.supervisor import CAUSE_EXIT, STATE_BACKOFF, Supervisor

if TYPE_CHECKING:
    from pathlib import Path

NOW = 1_000_000.0
CRASHER = "/bin/false"
FUTURE_DEADLINE = NOW + 600.0


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path / "home" / "runs"))


class _RecordingSupervisor(Supervisor):
    """Records the components start() was asked for."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        self.started: list[str] = []
        super().__init__(*args, **kwargs)

    def start(self, name: str, *, cause: str = CAUSE_EXIT) -> bool:  # type: ignore[override]
        self.started.append(name)
        return super().start(name, cause=cause)


def _config() -> ServeConfig:
    def spec(name: str, cmd: str | None) -> ComponentSpec:
        return ComponentSpec(name=name, command=cmd, crash_threshold=5, crash_window_minutes=15)

    return ServeConfig(
        operator="op",
        tick_seconds=0.01,
        shutdown_grace_s=0.3,
        components={
            "dispatcher": spec("dispatcher", CRASHER),
            "merger": spec("merger", None),
            "janitor": spec("janitor", None),
        },
        watchdog=WatchdogConfig(),
    )


def _save_backoff_state(operator: str, *, restart_due: float) -> None:
    write_json_atomic(
        state_path(operator),
        {
            "operator": operator,
            "updated_epoch": NOW,
            "children": {
                "dispatcher": {
                    "name": "dispatcher",
                    "state": STATE_BACKOFF,
                    "pid": None,
                    "starttime": None,
                    "restarts": 2,
                    "crash_epochs": [NOW - 30.0],
                    "last_exit_epoch": NOW - 30.0,
                    "last_exit_cause": "crash",
                    "last_exit_code": 3,
                    "pending_cause": CAUSE_EXIT,
                    "restart_due": restart_due,
                    "adopted": False,
                    "last_event_epoch": NOW - 30.0,
                    "no_progress_restarts": [],
                    "message": "restarting in 600s (cause crash)",
                }
            },
        },
    )


def test_a_restored_backoff_deadline_does_not_strand_a_component() -> None:
    _save_backoff_state("op", restart_due=FUTURE_DEADLINE)
    sup = _RecordingSupervisor("op", _config(), clock=FakeClock(start_time=NOW))
    try:
        state = sup.children["dispatcher"]
        assert state.state == STATE_BACKOFF, f"precondition: restored state, got {state}"
        assert state.restart_due == FUTURE_DEADLINE, "precondition: restored verbatim"

        sup.tick()

        assert "dispatcher" in sup.started, (
            f"the restored restart_due={FUTURE_DEADLINE!r} is 600s past the wall clock "
            f"the new supervisor reads ({sup.clock.time()!r}), so _ensure_running skips "
            f"the component on `restart_due > now` and nothing anywhere clears the "
            f"deadline: state={sup.children['dispatcher'].state!r}, started="
            f"{sup.started}. A backwards NTP step therefore parks a crash-looping "
            f"component in backoff forever, and serve status reports backoff with no "
            f"component running."
        )
    finally:
        sup.shutdown()


def test_control_a_due_backoff_deadline_does_start_the_component() -> None:
    """The gate itself is right; only an unrebased deadline strands a component."""
    _save_backoff_state("op", restart_due=NOW - 1.0)
    sup = _RecordingSupervisor("op", _config(), clock=FakeClock(start_time=NOW))
    try:
        sup.tick()
        assert "dispatcher" in sup.started, (
            f"a component whose backoff deadline has passed must be restarted; "
            f"started={sup.started}"
        )
    finally:
        sup.shutdown()
