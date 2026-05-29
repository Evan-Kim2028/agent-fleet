"""Process-group teardown kills the whole bridge tree, not just the sh wrapper."""

from __future__ import annotations

import subprocess
import time

from agent_fleet.bridge_daemon import _pid_alive, _terminate_process_group


def test_terminate_process_group_kills_grandchild() -> None:
    proc = subprocess.Popen(
        ["sh", "-c", "sleep 60 & child=$!; echo $child; wait"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        text=True,
    )
    try:
        assert proc.stdout is not None
        grandchild_pid = int(proc.stdout.readline().strip())
        assert _pid_alive(grandchild_pid)

        _terminate_process_group(proc)

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and _pid_alive(grandchild_pid):
            time.sleep(0.05)
        assert not _pid_alive(grandchild_pid), "node-equivalent grandchild leaked"
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            _terminate_process_group(proc)


def test_terminate_process_group_noop_on_dead_process() -> None:
    proc = subprocess.Popen(["true"], start_new_session=True)
    proc.wait()
    _terminate_process_group(proc)
    assert proc.poll() is not None
