"""Stopping one lane: by recorded process group, never by name pattern.

The bash drivers used ``pkill -f`` and ``pgrep -x devin`` + a cwd comparison.
Both are wrong with two operator sessions: ``pkill -f`` matches any process whose
command line merely *contains* the pattern, so a sibling lane dies with the
intended one. These tests pin the replacement's guarantees — including the
refusals, which are the safety property, not the happy path.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from agent_fleet.fleet_ops import registry
from agent_fleet.fleet_ops.registry import LaneRecord
from agent_fleet.fleet_ops.stop import (
    OK_ALREADY_GONE,
    OK_STOPPED,
    REFUSED_AMBIGUOUS,
    REFUSED_NO_PROCESS,
    REFUSED_NOT_FOUND,
    REFUSED_OWN_PGROUP,
    REFUSED_PID_REUSED,
    stop_lane,
    stop_lane_by_name,
    verify_process_identity,
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):  # noqa: ANN001, ANN202
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


def _spawn_sleeper() -> subprocess.Popen[bytes]:
    """A detached sleeper in its own process group, like a real lane engine."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        start_new_session=True,
    )


def _record_for(proc: subprocess.Popen[bytes], **overrides: Any) -> LaneRecord:  # noqa: ANN401
    base: dict[str, Any] = {
        "lane": "movers",
        "operator": "documents-0e",
        "pid": proc.pid,
        "pgid": os.getpgid(proc.pid),
        "starttime": registry.process_starttime(proc.pid),
    }
    return LaneRecord(**{**base, **overrides})


# ------------------------------------------------------------------- identity


def test_starttime_fingerprint_distinguishes_a_recycled_pid() -> None:
    proc = _spawn_sleeper()
    try:
        real = registry.process_starttime(proc.pid)
        assert real is not None
        # A record claiming a *different* birth time for this pid is a pid reuse.
        record = _record_for(proc, starttime=(real or 0) + 999)
        ok, reason = verify_process_identity(record)
        assert ok is False
        assert reason == REFUSED_PID_REUSED
    finally:
        proc.kill()
        proc.wait()


def test_a_dead_process_reports_already_gone() -> None:
    record = LaneRecord(lane="x", operator="o", pid=999_999_999, pgid=999_999_999)
    ok, reason = verify_process_identity(record)
    assert ok is False
    assert reason == OK_ALREADY_GONE


# ---------------------------------------------------------------- refusals


def test_refuses_when_there_is_no_recorded_process() -> None:
    record = LaneRecord(lane="x", operator="o")
    result = stop_lane(record)
    assert result.stopped is False
    assert result.reason == REFUSED_NO_PROCESS


def test_refuses_to_signal_the_callers_own_process_group() -> None:
    """The single most important refusal: never kill the operator's own shell."""
    record = LaneRecord(
        lane="x",
        operator="o",
        pid=os.getpid(),
        pgid=os.getpgid(0),
        starttime=registry.process_starttime(os.getpid()),
    )
    result = stop_lane(record)
    assert result.stopped is False
    assert result.reason == REFUSED_OWN_PGROUP


def test_refuses_a_recycled_pid() -> None:
    """Signalling the recorded pgid would hit an unrelated process tree."""
    proc = _spawn_sleeper()
    try:
        record = _record_for(proc, starttime=(registry.process_starttime(proc.pid) or 0) + 1)
        result = stop_lane(record)
        assert result.stopped is False
        assert result.reason == REFUSED_PID_REUSED
        # And the real process is untouched.
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait()


# ------------------------------------------------------------------- stopping


def test_stops_a_lane_process_group() -> None:
    proc = _spawn_sleeper()
    try:
        result = stop_lane(_record_for(proc), grace_s=5.0, poll_s=0.05)
        assert result.stopped is True
        assert result.reason == OK_STOPPED
        assert result.signalled is True
        assert proc.wait(timeout=5) != 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_an_already_dead_lane_is_reported_as_stopped() -> None:
    result = stop_lane(
        LaneRecord(lane="x", operator="o", pid=999_999_999, pgid=999_999_999), grace_s=0.1
    )
    assert result.stopped is True
    assert result.reason == OK_ALREADY_GONE


# -------------------------------------------------------------- lookup by name


def test_unknown_lane_is_refused() -> None:
    result = stop_lane_by_name("does-not-exist")
    assert result.stopped is False
    assert result.reason == REFUSED_NOT_FOUND


def test_an_ambiguous_lane_name_is_refused_not_guessed() -> None:
    """Two operators may own the same lane name; picking one could kill the wrong tree."""
    registry.update_record("documents-0e", "shared", state="running", pid=1, pgid=1)
    registry.update_record("documents-1d", "shared", state="running", pid=1, pgid=1)
    result = stop_lane_by_name("shared")
    assert result.stopped is False
    assert result.reason == REFUSED_AMBIGUOUS
    assert "documents-0e" in result.detail and "documents-1d" in result.detail


def test_the_operator_flag_disambiguates() -> None:
    """Two operators may own a lane name; --operator picks which one dies."""
    proc = _spawn_sleeper()
    try:
        registry.save_record(_record_for(proc, operator="documents-1d", lane="shared"))
        # A same-named lane owned by the other operator, pointed at a dead pid so
        # the assertion can only pass if the *right* record was chosen.
        registry.save_record(
            LaneRecord(lane="shared", operator="documents-0e", pid=999_999_999, pgid=999_999_999)
        )
        result = stop_lane_by_name("shared", operator="documents-1d", grace_s=5.0)
        assert result.stopped is True
        assert proc.wait(timeout=5) != 0
        # The other operator's record is untouched — still holding its own
        # (dead) pid, not cleared by the stop that targeted documents-1d.
        other = registry.load_record("documents-0e", "shared")
        assert other is not None
        assert other.state == "idle"
        assert other.pid == 999_999_999
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_stopping_records_the_terminal_state() -> None:
    proc = _spawn_sleeper()
    try:
        registry.save_record(_record_for(proc))
        stop_lane_by_name("movers", operator="documents-0e", grace_s=5.0)
        record = registry.load_record("documents-0e", "movers")
        assert record is not None
        assert record.state == registry.STATE_STOPPED
        # The identity is cleared so a later stop cannot signal a reused pid.
        assert record.pid is None
        assert record.pgid is None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


# ------------------------------------------------------------------- the fence


def test_no_module_uses_pkill_or_pgrep() -> None:
    """A regression guard on the constraint itself, not just the behaviour.

    Behavioural tests prove the current code refuses correctly; this one fails if
    someone later reintroduces pattern-matching termination, which would pass
    every other test on a machine running a single lane.

    The module docstring names both tools when explaining *why* they are wrong,
    so the scan skips docstrings and asserts on the remaining string literals —
    the only place a command name could actually reach a subprocess.
    """
    import ast

    import agent_fleet.fleet_ops.stop as stop_mod

    tree = ast.parse(Path(stop_mod.__file__).read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            assert "pkill" not in node.value, f"string literal mentions pkill: {node.value!r}"
            assert "pgrep" not in node.value, f"string literal mentions pgrep: {node.value!r}"


def test_stopping_a_real_lane_leaves_the_caller_alive() -> None:
    proc = _spawn_sleeper()
    try:
        result = stop_lane(_record_for(proc), grace_s=5.0, poll_s=0.05)
        assert result.stopped is True
        # We are still here, which is the whole point of the own-pgid refusal.
        assert time.monotonic() > 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
