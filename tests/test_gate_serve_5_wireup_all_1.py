"""`fleet serve stop` must actually stop the supervisor.

The supervisor installs a SIGTERM handler that only flips a flag
(``Supervisor.reaper_signals`` -> ``self._stopping = True``), and the flag is
documented as the only thing the handler does, on purpose: a handler that waits
on children can deadlock against the syscall that interrupted it.

For that to be sound, the loop has to *read* the flag and return. Every other
layer does: ``Supervisor.tick`` bails on ``_stopping`` twice, and
``Supervisor.shutdown`` sets it. ``ServeLoop._loop`` — the function that
actually runs forever, and the one the ``finally`` in ``run`` hangs off — never
looks at it. So the flag is set by a signal nobody consumes, and ``_loop``
ticks until the operator resorts to SIGKILL, which the ``finally`` never reaches.

The operator-visible consequence is worse than a slow stop: ``fleet serve stop``
prints "sent SIGTERM to the supervisor" and exits 0 the moment the signal is
delivered (``ServeLoop.stop`` returns ``result.signalled``, which only means
"``os.kill`` did not raise"). The process is still running, and the exclusive
flock on ``serve/<operator>/serve.lock`` is still held, so the next
``fleet serve run`` for that operator exits 3 "already running" and the only
way out is ``kill -9``.

Two tests, same defect: one on the loop contract, one end to end against a real
process holding a real flock.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.clock import SystemClock
from agent_fleet.serve.config import ServeConfig
from agent_fleet.serve.serve import ServeLoop

if TYPE_CHECKING:
    from pathlib import Path

#: No enabled components on purpose. The defect is that the loop never returns,
#: which has nothing to do with whether a child is attached, and a componentless
#: supervisor leaves no process behind for the test to clean up.
CONFIG = ServeConfig(operator="op", tick_seconds=0.2, shutdown_grace_s=0.5)

#: What `fleet serve` actually executes: a supervisor with no component commands,
#: so the only thing it does is take the flock and tick.
CHILD_CODE = (
    "from agent_fleet.serve.config import ServeConfig\n"
    "from agent_fleet.serve.serve import ServeLoop\n"
    "cfg = ServeConfig(operator='op', tick_seconds=0.2, shutdown_grace_s=0.5)\n"
    "raise SystemExit(ServeLoop(operator='op', config=cfg).run())\n"
)

#: Generous enough that a loaded box is not what fails this test.
JOIN_TIMEOUT_S = 5.0


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path / "home" / "runs"))


def _wait_until(predicate: object, timeout: float) -> bool:
    """Bounded poll. Never blocks past *timeout* — no bare `while True`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if callable(predicate) and predicate():
            return True
        time.sleep(0.02)
    return False


def test_loop_returns_after_sigterm(tmp_path: Path) -> None:
    """The loop must observe the stop flag the signal handler set and return.

    Real signal delivery to this very process, a real ``SystemClock`` and the
    real handler installed by ``reaper_signals`` — only the loop runs on a
    thread, because it is the one thing under test that has to be interruptible
    from the outside.
    """
    loop = ServeLoop(
        operator="op",
        config=CONFIG,
        clock=SystemClock(),
        cgroup_root=tmp_path / "cg",
        max_ticks=None,
    )
    # A cgroup root that does not exist only makes the tick read as degraded.
    results: list[int] = []
    previous: dict[int, object] = loop.supervisor.reaper_signals()
    thread = threading.Thread(target=lambda: results.append(loop._loop()), daemon=True)
    try:
        thread.start()
        assert _wait_until(lambda: loop._tick_index >= 1, 5.0), "the loop never ticked"
        assert loop.supervisor._stopping is False

        os.kill(os.getpid(), signal.SIGTERM)
        assert _wait_until(lambda: loop.supervisor._stopping, 5.0), (
            "the SIGTERM handler did not run; the test cannot conclude anything"
        )

        thread.join(timeout=JOIN_TIMEOUT_S)
        assert not thread.is_alive(), (
            "ServeLoop._loop ignored supervisor._stopping: SIGTERM set the flag but the "
            f"loop kept ticking (tick index {loop._tick_index}, still running after "
            f"{JOIN_TIMEOUT_S}s) instead of returning, so run()'s finally — which shuts "
            "children down, saves state and clears the pid file — never runs and the "
            "flock on serve.lock is never released"
        )
        assert results == [0], "the loop must return the process exit code"
    finally:
        # The loop ignores SIGTERM by design here, so it has to be unwound
        # explicitly or the thread outlives the test and writes into a
        # tmp_path pytest has already removed. The assertion above has already
        # recorded the verdict, so this cannot mask it.
        loop.max_ticks = loop._tick_index + 1
        thread.join(timeout=10.0)
        for sig, handler in previous.items():
            if handler is not None:
                signal.signal(sig, handler)


def test_a_sigtermed_supervisor_exits_and_frees_the_operator_lock() -> None:
    """End to end, against a real process holding a real flock.

    This is what the operator sees: ``fleet serve stop`` returns success as soon
    as the signal is delivered, so the only way to tell whether the supervisor
    actually stopped is to watch the process and the lock.
    """
    from agent_fleet.serve.paths import exclusive_lock, lock_path, pid_path

    child = subprocess.Popen([sys.executable, "-c", CHILD_CODE])
    try:
        # The pid file is only written after the flock is taken.
        assert _wait_until(lambda: pid_path("op").exists(), 30.0), "the supervisor never started"

        # The child really does hold the lock, or this test would prove nothing.
        with exclusive_lock(lock_path("op")) as acquired:
            assert acquired is False, "the running supervisor should be holding serve.lock"

        os.kill(child.pid, signal.SIGTERM)
        assert _wait_until(lambda: child.poll() is not None, JOIN_TIMEOUT_S), (
            f"the supervisor was still running {JOIN_TIMEOUT_S}s after SIGTERM (pid "
            f"{child.pid}); `fleet serve stop` would already have reported success and "
            "left the operator lock held, so the next `fleet serve run` exits 3"
        )

        with exclusive_lock(lock_path("op")) as acquired:
            assert acquired is True, "serve.lock must be free once the supervisor exits"
    finally:
        if child.poll() is None:
            # Only the process this test started.
            child.kill()
            child.wait(timeout=10)
