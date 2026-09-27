"""prodsafety-1: a stop signal must interrupt a tick, not queue behind its backoff.

``tick()`` pays the restart backoff inline (``self.clock.sleep(delay)``,
supervisor.py:582), and with ``SystemClock`` that is ``time.sleep``. The signal
handler installed by ``reaper_signals()`` only flips ``self._stopping``
(supervisor.py:773-774), and the loop does not look at it again until the sleep
returns. So a SIGTERM arriving mid-tick is ignored for the whole delay -- up to
``backoff_max_s`` (300s by default) per component, per restart, and the sum over
every crash-looping component.

A systemd ``systemctl stop`` under the default ``TimeoutStopSec=90s`` therefore
SIGKILLs the supervisor while it is asleep, and ``shutdown()`` never runs at
all: the components are orphaned and every pid file the next supervisor finds
names a corpse.

The test drives ``tick()`` in a child process, sends SIGTERM 2s in, and asserts
the child notices the stop promptly -- well inside the 8s backoff.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest

CRASHER = f"{sys.executable} -c 'import sys; sys.exit(3)'"
BACKOFF_INITIAL_S = 8.0
BACKOFF_MAX_S = 300.0

DRIVER = """
import os, signal, sys, time
sys.path.insert(0, {repo!r})
from agent_fleet.serve.clock import SystemClock
from agent_fleet.serve.config import ComponentSpec, ServeConfig, WatchdogConfig
from agent_fleet.serve.supervisor import Supervisor

REPO = {repo!r}
CRASHER = {crasher!r}

def spec(name, cmd):
    return ComponentSpec(
        name=name, command=cmd,
        backoff_initial_s={backoff!r}, backoff_max_s={backoff_max!r},
        crash_threshold=100, crash_window_minutes=15,
    )

config = ServeConfig(
    operator="op", tick_seconds=0.01, shutdown_grace_s=0.5,
    components={{
        "dispatcher": spec("dispatcher", CRASHER),
        "merger": spec("merger", None),
        "janitor": spec("janitor", None),
    }},
    watchdog=WatchdogConfig(),
)
sup = Supervisor("op", config, clock=SystemClock())
sup.reaper_signals()
sup.start("dispatcher")

# Give the crash time to land so tick() definitely pays the backoff.
deadline = time.monotonic() + 10
while time.monotonic() < deadline:
    if sup._procs.get("dispatcher") is not None and sup._procs["dispatcher"].poll() is not None:
        break
    time.sleep(0.02)

with open({ready!r}, "w", encoding="utf-8") as handle:
    handle.write(str(os.getpid()))

tick_done = time.monotonic()
sup.tick()
with open({done!r}, "w", encoding="utf-8") as handle:
    handle.write(repr(time.monotonic() - tick_done))

if sup._stopping:
    sup.shutdown()
    with open({stopped!r}, "w", encoding="utf-8") as handle:
        handle.write("yes")
"""


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path / "home" / "runs"))


def _wait_for(path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return False


def test_a_stop_signal_is_honoured_within_a_second_of_a_blocking_backoff(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "ready"
    done = tmp_path / "tick-done"
    stopped = tmp_path / "stopped"
    driver = DRIVER.format(
        repo=str(Path(__file__).resolve().parent.parent),
        crasher=CRASHER,
        backoff=BACKOFF_INITIAL_S,
        backoff_max=BACKOFF_MAX_S,
        ready=str(ready),
        done=str(done),
        stopped=str(stopped),
    )
    script = tmp_path / "driver.py"
    script.write_text(driver, encoding="utf-8")

    child = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert _wait_for(ready, 30.0), f"driver never got going: {child.communicate()[1]!r}"

    signalled_at = time.monotonic()
    os.kill(child.pid, signal.SIGTERM)

    saw_stop = _wait_for(stopped, 3.0)
    try:
        if not saw_stop:
            elapsed = time.monotonic() - signalled_at
            child.wait(timeout=BACKOFF_INITIAL_S + 10)
            observed = done.read_text(encoding="utf-8") if done.exists() else "<no tick-done>"
            pytest.fail(
                f"the supervisor ran for {elapsed:.2f}s after SIGTERM and only then ran "
                f"shutdown() (tick() took {observed}s), because tick() is asleep inside "
                f"clock.sleep({BACKOFF_INITIAL_S}) and the _stopping flag is not read "
                f"until it returns. Under systemd's TimeoutStopSec the supervisor is "
                f"SIGKILLed mid-sleep and shutdown() never runs, orphaning components "
                f"and leaving pid files behind for the next supervisor to adopt."
            )
    finally:
        # Only ever signal a pid this test spawned.
        if child.poll() is None:
            with suppress(OSError):
                os.kill(child.pid, signal.SIGKILL)
            child.wait(timeout=10)

    assert stopped.exists(), "shutdown() never ran after the stop flag was observed"
    assert done.exists(), "precondition: the blocking tick() completed"
