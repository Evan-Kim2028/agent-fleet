"""Process-safety invariants — the rules that keep the watchdog safe.

This machine runs dozens of other agents. A self-healing supervisor that
mistakes someone else's process for a stuck lane is worse than no supervisor,
so every rule below is a rule about *not* signalling something.

The three that a first draft got wrong, and that these tests exist to pin:

* an unreaped zombie looks exactly like a live process in ``/proc``, and
  reporting it alive makes ``terminate`` claim it killed a corpse;
* a recycled pid carries a live process that has nothing to do with the fleet;
* a group signal to a pid that is not its own group leader reaches processes
  the fleet never spawned.

The real-child tests are not optional. A mocked runner cannot produce a zombie,
cannot produce a recycled pid, and cannot produce a process group, so every
one of those cases would go untested under injection alone.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.procs import (
    ProcIdentity,
    boot_time,
    escalate_kill,
    escalate_kill_group,
    owns,
    parent_pid,
    pid_alive,
    process_state,
    starttime_fingerprint,
    terminate,
    terminate_group,
)

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class _FakeStat:
    st_mtime: float


def _fake_proc_root(tmp_path: Path, pid: int, *, state: str = "S", ppid: int = 1) -> Path:
    """A fabricated /proc tree: <root>/<pid>/stat, as the kernel writes it.

    The format is `pid (comm) state ppid ...`, and ``comm`` may itself contain
    spaces and parentheses — which is why every parser here splits on the last
    ``)``. The comm below has a space on purpose, so a parser splitting on the
    first ``)`` gets the wrong field count.

    After the ``)`` the fields are 3..52, so ppid (field 4) is index 1 and
    starttime (field 22) is index 19.
    """
    root = tmp_path / "proc"
    fields = [state, str(ppid)]
    fields += ["0"] * 18  # fields 5..22, giving 20 entries after comm
    fields[19] = "987654"  # starttime, field 22 == index 19 after comm
    (root / str(pid)).mkdir(parents=True, exist_ok=True)
    (root / str(pid) / "stat").write_text(
        f"{pid} (my proc ess) {' '.join(fields)}\n", encoding="utf-8"
    )
    return root


def _spawn(code: int = 0) -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-c", f"import sys; sys.exit({code})"])


# ------------------------------------------------------------------ fingerprints


def test_starttime_fingerprint_parses_field_22_despite_spaces_in_comm(tmp_path: Path) -> None:
    root = _fake_proc_root(tmp_path, 4242)
    assert starttime_fingerprint(4242, proc_root=root) == 987654


def test_starttime_fingerprint_is_none_for_a_missing_pid(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    assert starttime_fingerprint(9999, proc_root=root) is None


def test_starttime_fingerprint_is_none_for_pid_zero(tmp_path: Path) -> None:
    assert starttime_fingerprint(0, proc_root=tmp_path) is None


def test_starttime_fingerprint_is_none_for_a_truncated_stat(tmp_path: Path) -> None:
    root = tmp_path / "proc"
    (root / "77").mkdir(parents=True)
    (root / "77" / "stat").write_text("77 (short) S 1\n", encoding="utf-8")
    assert starttime_fingerprint(77, proc_root=root) is None


# ------------------------------------------------------------------------ liveness


def test_pid_alive_is_false_for_a_missing_pid(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    assert pid_alive(1234, proc_root=root) is False


def test_pid_alive_is_false_for_a_zombie(tmp_path: Path) -> None:
    """The bug this pins: a zombie sits in /proc looking exactly like a live process.

    A supervisor that trusts /proc existence alone will report a finished child
    as running, skip the restart it owes, and — when the watchdog does act —
    log a successful termination of a process that exited on its own.
    """
    root = _fake_proc_root(tmp_path, 555, state="Z")
    assert process_state(555, proc_root=root) == "Z"
    assert pid_alive(555, proc_root=root) is False


def test_pid_alive_is_false_for_a_dead_x_state(tmp_path: Path) -> None:
    root = _fake_proc_root(tmp_path, 556, state="X")
    assert pid_alive(556, proc_root=root) is False


def test_pid_alive_is_true_for_a_sleeping_process(tmp_path: Path) -> None:
    root = _fake_proc_root(tmp_path, 557, state="S")
    assert pid_alive(557, proc_root=root) is True


def test_pid_alive_is_true_when_state_is_unreadable(tmp_path: Path) -> None:
    """Unreadable stat means the process exists but we cannot see it.

    Assuming dead would be the dangerous direction: it invites acting on a pid
    whose identity we never verified.
    """
    root = tmp_path / "proc"
    (root / "558").mkdir(parents=True)
    assert pid_alive(558, proc_root=root) is True


def test_pid_alive_is_false_for_a_real_unreaped_zombie() -> None:
    """The same thing, against a real process this test created.

    Spawned and deliberately NOT reaped: the whole point is that the kernel
    keeps the entry around. This is the case a mocked runner cannot produce.
    """
    proc = _spawn(0)
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and process_state(proc.pid) != "Z":
            time.sleep(0.02)
        assert process_state(proc.pid) == "Z", "expected the child to be a zombie"
        assert pid_alive(proc.pid) is False, "a zombie must not count as alive"
    finally:
        proc.wait(timeout=5)


def test_pid_alive_is_true_for_a_live_real_child() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert pid_alive(proc.pid) is True
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_pid_alive_is_false_for_none_and_nonpositive() -> None:
    assert pid_alive(None) is False
    assert pid_alive(0) is False
    assert pid_alive(-1) is False


# ------------------------------------------------------------------ identity gate


def test_identity_matches_a_live_process_with_the_same_fingerprint() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        identity = ProcIdentity(pid=proc.pid, starttime=starttime_fingerprint(proc.pid))
        assert identity.matches() is True
        assert owns(identity) is True
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_identity_does_not_match_after_the_process_exits() -> None:
    proc = _spawn(0)
    proc.wait(timeout=5)
    identity = ProcIdentity(pid=proc.pid, starttime=starttime_fingerprint(proc.pid))
    assert identity.matches() is False


def test_identity_without_a_fingerprint_never_matches() -> None:
    """A pid with no recorded start time is not an identity.

    Without the fingerprint there is no way to tell the original process from a
    recycled pid, so the only safe answer is to refuse.
    """
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert ProcIdentity(pid=proc.pid, starttime=None).matches() is False
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_identity_does_not_match_a_recycled_pid(tmp_path: Path) -> None:
    """A pid the kernel handed to someone else must stop matching."""
    root = _fake_proc_root(tmp_path, 9001, state="S")
    # The process is alive, but its start time is no longer the recorded one.
    (root / "9001" / "stat").write_text(
        (root / "9001" / "stat").read_text(encoding="utf-8").replace("987654", "111111"),
        encoding="utf-8",
    )
    recycled = ProcIdentity(pid=9001, starttime=987654)
    assert pid_alive(9001, proc_root=root) is True
    assert recycled.matches(proc_root=root) is False, "a recycled pid must not match"
    assert owns(recycled, proc_root=root) is False


def test_owns_of_none_is_false() -> None:
    assert owns(None) is False


def test_identity_from_dict_rejects_a_missing_or_unusable_record() -> None:
    assert ProcIdentity.from_dict({}) is None
    assert ProcIdentity.from_dict({"pid": 0}) is None
    assert ProcIdentity.from_dict({"pid": "x"}) is None
    # pid without a fingerprint is not usable as an identity
    assert ProcIdentity.from_dict({"pid": 12}) is None
    parsed = ProcIdentity.from_dict({"pid": 12, "starttime": 34})
    assert parsed == ProcIdentity(pid=12, starttime=34)


# ------------------------------------------------------------------- termination


def test_terminate_refuses_a_pid_the_fleet_never_recorded() -> None:
    """No recorded identity -> no signal. This is the whole safety argument."""
    result = terminate(None)
    assert result.signalled is False
    assert result.skipped_reason == "no recorded process identity"


def test_terminate_refuses_an_identity_without_a_fingerprint() -> None:
    result = terminate(ProcIdentity(pid=os.getpid(), starttime=None))
    assert result.signalled is False
    assert "fingerprint" in (result.skipped_reason or "")


def test_terminate_refuses_a_recycled_pid() -> None:
    proc = _spawn(0)
    proc.wait(timeout=5)
    result = terminate(ProcIdentity(pid=proc.pid, starttime=12345))
    assert result.signalled is False
    assert "recycled" in (result.skipped_reason or "")


def test_terminate_signals_a_real_owned_child() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    identity = ProcIdentity(pid=proc.pid, starttime=starttime_fingerprint(proc.pid))
    result = terminate(identity)
    assert result.signalled is True
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - TERM should land
        proc.kill()
        proc.wait(timeout=5)
        pytest.fail("TERM did not terminate the child")


def test_terminate_does_not_wait_and_does_not_escalate() -> None:
    """TERM only, in one call — the grace is the caller's per-tick budget.

    A terminate() that slept would make a watchdog tick block once per stale
    pid, and a watchdog that blocks falls behind and trips its own no-progress
    rule.
    """
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(30)",
        ]
    )
    identity = ProcIdentity(pid=proc.pid, starttime=starttime_fingerprint(proc.pid))
    try:
        started = time.monotonic()
        result = terminate(identity)
        elapsed = time.monotonic() - started
        assert result.signalled is True
        assert result.escalated is False
        assert elapsed < 1.0, "terminate() must not sleep"
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_escalate_kill_terminates_a_child_that_ignores_term() -> None:
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(30)",
        ]
    )
    identity = ProcIdentity(pid=proc.pid, starttime=starttime_fingerprint(proc.pid))
    try:
        assert terminate(identity).signalled is True
        result = escalate_kill(identity)
        assert result.signalled is True
        assert result.escalated is True
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_escalate_kill_reports_a_skip_for_an_exited_process() -> None:
    proc = _spawn(0)
    proc.wait(timeout=5)
    identity = ProcIdentity(pid=proc.pid, starttime=starttime_fingerprint(proc.pid))
    result = escalate_kill(identity)
    assert result.signalled is False


# ---------------------------------------------------------------- group signals


def test_terminate_group_kills_a_session_leader_and_its_children() -> None:
    """A child spawned with start_new_session is its own group leader.

    The grandchild here exists to prove the group signal reaches what the
    fleet spawned and not just the direct child.
    """
    script = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    identity = ProcIdentity(pid=proc.pid, starttime=starttime_fingerprint(proc.pid))
    try:
        assert os.getpgid(proc.pid) == proc.pid, "expected a group leader"
        result = terminate_group(identity)
        assert result.signalled is True
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=5)


def test_terminate_group_refuses_a_non_group_leader() -> None:
    """A pid that shares a group is unkillable by the safe path — and must be.

    Signalling its group would reach every process in this test runner's group,
    which is exactly the blast radius the group check exists to prevent.
    """
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    identity = ProcIdentity(pid=proc.pid, starttime=starttime_fingerprint(proc.pid))
    try:
        assert os.getpgid(proc.pid) != proc.pid
        result = terminate_group(identity)
        assert result.signalled is False
        assert "not a group leader" in (result.skipped_reason or "")
        assert proc.poll() is None, "the process must be untouched"
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_escalate_kill_group_refuses_a_non_group_leader() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    identity = ProcIdentity(pid=proc.pid, starttime=starttime_fingerprint(proc.pid))
    try:
        result = escalate_kill_group(identity)
        assert result.signalled is False
        assert "not a group leader" in (result.skipped_reason or "")
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_terminate_group_refuses_a_recycled_pid() -> None:
    proc = _spawn(0)
    proc.wait(timeout=5)
    result = terminate_group(ProcIdentity(pid=proc.pid, starttime=999999))
    assert result.signalled is False


def test_terminate_group_refuses_none() -> None:
    assert terminate_group(None).skipped_reason == "no recorded process identity"


# ------------------------------------------------------------------- /proc reads


def test_parent_pid_reads_field_4(tmp_path: Path) -> None:
    root = _fake_proc_root(tmp_path, 314, ppid=271)
    assert parent_pid(314, proc_root=root) == 271


def test_parent_pid_is_none_for_a_missing_pid(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    assert parent_pid(4242, proc_root=root) is None


def test_boot_time_uses_btime_plus_starttime_ticks(tmp_path: Path) -> None:
    """An orphan's age cannot come from a file serve wrote, so it comes from here.

    The legacy watchdog aged orphans by scanning the process list for an engine
    name; this reads /proc/<pid>/stat field 22 against /proc/stat btime, which
    is the only source that works for a process whose parent is gone and which
    the fleet therefore has no other handle on.
    """
    root = _fake_proc_root(tmp_path, 5555)
    (root / "stat").write_text("cpu 1 2 3\nbtime 1000000\nprocesses 10\n", encoding="utf-8")
    started = boot_time(5555, proc_root=root)
    assert started is not None
    ticks = os.sysconf("SC_CLK_TCK")
    assert started == pytest.approx(1_000_000 + 987654 / ticks)


def test_boot_time_is_none_without_btime(tmp_path: Path) -> None:
    root = _fake_proc_root(tmp_path, 5556)
    (root / "stat").write_text("cpu 1 2 3\n", encoding="utf-8")
    assert boot_time(5556, proc_root=root) is None


def test_fake_stat_helper_is_used_by_the_clock_pattern() -> None:
    assert _FakeStat(st_mtime=5.0).st_mtime == 5.0
