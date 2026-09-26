"""correctness-3: a log file that cannot be opened must not raise UnboundLocalError.

``Supervisor.start`` opens the component log inside the same ``try`` that guards
``subprocess.Popen`` (supervisor.py:380-407). The handler is::

    except (OSError, ValueError) as exc:
        handle.close()

``handle`` is bound on the line before, so an ``OSError`` raised *by
``log_path.open("ab")`` itself leaves it unbound and ``handle.close()`` raises
``UnboundLocalError`` -- which escapes the spawn-failure path the code
implements on purpose. Instead of recording a crash, emitting
``serve.component.spawn_failed`` and letting the ensure-running loop charge the
crash budget, the exception propagates out of ``tick()`` and takes the whole
supervisor with it.

A directory where the log file belongs reproduces it exactly: ``open("ab")``
raises ``IsADirectoryError`` before any spawn is attempted.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.config import ComponentSpec, ServeConfig, WatchdogConfig
from agent_fleet.serve.events import read_serve_events
from agent_fleet.serve.paths import component_log_path
from agent_fleet.serve.supervisor import CAUSE_CRASH, STATE_RUNNING, Supervisor

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

SLEEPER = "sleep 300"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path / "home" / "runs"))


def _config(command: str = SLEEPER) -> ServeConfig:
    def spec(name: str, cmd: str | None) -> ComponentSpec:
        return ComponentSpec(name=name, command=cmd, crash_threshold=3, crash_window_minutes=15)

    return ServeConfig(
        operator="op",
        tick_seconds=0.01,
        shutdown_grace_s=0.3,
        components={
            "dispatcher": spec("dispatcher", command),
            "merger": spec("merger", None),
            "janitor": spec("janitor", None),
        },
        watchdog=WatchdogConfig(),
    )


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_an_unopenable_component_log_is_recorded_as_a_failed_spawn() -> None:
    # A directory where the log file belongs: open("ab") fails with
    # IsADirectoryError, an OSError, before Popen is ever reached.
    log_path = component_log_path("op", "dispatcher")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.mkdir()

    sup = Supervisor("op", _config(), clock=FakeClock())
    spawned: list[int] = []
    try:
        try:
            started = sup.start("dispatcher")
        except Exception as exc:
            pytest.fail(
                f"Supervisor.start() raised {type(exc).__name__}: {exc} instead of "
                f"recording the failed spawn. log_path.open('ab') is inside the try "
                f"that guards Popen, so an OSError from the open lands in a handler "
                f"that calls handle.close() on an unbound local."
            )

        state = sup.children["dispatcher"]
        assert started is False, "a component whose log cannot be opened must not start"
        assert state.crashes_in_window(sup.clock.time(), 900.0) == 1, (
            f"the failed spawn was not charged to the crash budget: {state.to_dict()}"
        )
        assert state.last_exit_cause == CAUSE_CRASH
        assert _wait_until(
            lambda: any(
                event.get("event") == "serve.component.spawn_failed"
                for event in read_serve_events("op")
            )
        ), "no serve.component.spawn_failed event was emitted"

        # The whole point of the path: the ensure-running loop keeps going and
        # spends the budget instead of the supervisor dying.
        sup.tick()
        assert sup.children["dispatcher"].state != STATE_RUNNING
    finally:
        if "dispatcher" in sup._procs:
            spawned.append(sup._procs["dispatcher"].pid)
        sup.shutdown()
        for pid in spawned:
            assert pid > 0
