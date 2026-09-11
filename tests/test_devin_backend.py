"""Devin CLI backend — unit tests (mocked subprocess; no live binary)."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agent_fleet.devin_backend import (
    DEFAULT_MODEL,
    DevinBackend,
    DevinLLMResult,
    DevinSession,
    _DevinErrorSession,
    _harvest_devin_usage,
    _read_export_usage,
    _read_sessions_db_usage,
    call_devin,
    check_devin_auth,
    classify_devin_error,
)

# --- Constants / registry --------------------------------------------------


def test_default_model_is_swe_2_high() -> None:
    assert DEFAULT_MODEL == "swe-2-high"


def test_devin_resolves_from_registry() -> None:
    from agent_fleet.backends import make_backend
    from agent_fleet.config import FleetConfig

    cfg = FleetConfig(default_backend="devin", default_model=None)
    backend = make_backend(cfg)
    assert isinstance(backend, DevinBackend)


def test_devin_env_var_is_none() -> None:
    from agent_fleet.backends import backend_env_var

    assert backend_env_var("devin") is None


def test_devin_auth_probe_present() -> None:
    from agent_fleet.backends import backend_auth_probe

    probe = backend_auth_probe("devin")
    assert probe is not None
    assert callable(probe)


def test_devin_backend_default_model_from_registry() -> None:
    from agent_fleet.backends import backend_default_model

    assert backend_default_model("devin") == "swe-2-high"


def test_devin_factory_respects_explicit_model_and_bin() -> None:
    from agent_fleet.backends import make_backend
    from agent_fleet.config import FleetConfig

    cfg = FleetConfig(
        default_backend="devin", default_model="claude-sonnet-5", devin_bin="/bin/devin"
    )
    backend = make_backend(cfg)
    assert backend.model == "claude-sonnet-5"
    assert backend.devin_bin == "/bin/devin"


# --- check_devin_auth -------------------------------------------------------


def test_check_devin_auth_fails_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "agent_fleet.devin_backend._find_devin_bin", lambda: str(tmp_path / "no-such-devin")
    )
    monkeypatch.setattr("agent_fleet.devin_backend.shutil.which", lambda _: None)
    ok, detail, _fix = check_devin_auth()
    assert ok is False
    assert "not found" in detail.lower() or "missing" in detail.lower()


def test_check_devin_auth_fails_when_credentials_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bin_path = tmp_path / "devin"
    bin_path.write_text("#!/bin/sh\n", encoding="utf-8")
    bin_path.chmod(0o755)
    monkeypatch.setattr("agent_fleet.devin_backend.shutil.which", lambda _: str(bin_path))
    monkeypatch.setattr(
        "agent_fleet.devin_backend.CREDENTIALS_PATH", tmp_path / "missing-credentials.toml"
    )
    ok, detail, fix = check_devin_auth()
    assert ok is False
    assert "missing" in detail.lower()
    assert "devin auth login" in fix


def test_check_devin_auth_fails_when_key_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bin_path = tmp_path / "devin"
    bin_path.write_text("#!/bin/sh\n", encoding="utf-8")
    bin_path.chmod(0o755)
    creds = tmp_path / "credentials.toml"
    creds.write_text('windsurf_api_key = ""\n', encoding="utf-8")
    monkeypatch.setattr("agent_fleet.devin_backend.shutil.which", lambda _: str(bin_path))
    monkeypatch.setattr("agent_fleet.devin_backend.CREDENTIALS_PATH", creds)
    ok, detail, _fix = check_devin_auth()
    assert ok is False
    assert "windsurf_api_key" in detail


def test_check_devin_auth_passes_with_valid_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bin_path = tmp_path / "devin"
    bin_path.write_text("#!/bin/sh\n", encoding="utf-8")
    bin_path.chmod(0o755)
    creds = tmp_path / "credentials.toml"
    creds.write_text('windsurf_api_key = "wf-secret-token"\n', encoding="utf-8")
    monkeypatch.setattr("agent_fleet.devin_backend.shutil.which", lambda _: str(bin_path))
    monkeypatch.setattr("agent_fleet.devin_backend.CREDENTIALS_PATH", creds)
    ok, detail, fix = check_devin_auth()
    assert ok is True
    assert "authenticated" in detail.lower()
    assert fix == ""


# --- classify_devin_error ----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Rate limited: try again later", "rate_limit"),
        ("Quota exhausted: upgrade your plan", "quota"),
        ("Usage limit reached for this month", "quota"),
        ("Usage paused, contact support", "quota"),
        ("Server error: 500 from upstream", "transient"),
        ("Connection failed (attempt 2/5)", "transient"),
        ("Request timed out: no response", "timeout"),
        ("some unrelated stack trace", None),
        ("", None),
    ],
)
def test_classify_devin_error(text: str, expected: str | None) -> None:
    assert classify_devin_error(text) == expected


# --- call_devin: fresh / resume argv -----------------------------------------


def _fake_completed(stdout: str = "ok", stderr: str = "", returncode: int = 0) -> Any:  # noqa: ANN401
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _write_export(cmd: list[str], session_id: str) -> None:
    idx = cmd.index("--export")
    path = Path(cmd[idx + 1])
    path.write_text(f'{{"session_id": "{session_id}"}}', encoding="utf-8")


def test_call_devin_fresh_run_builds_argv(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def _run(cmd: list[str], **kwargs: Any) -> Any:  # noqa: ANN401
        captured["cmd"] = list(cmd)
        captured["env"] = kwargs.get("env")
        _write_export(cmd, "fresh-session")
        return _fake_completed("hello")

    text, session_id, usage, code = call_devin(
        "do it",
        work_dir=str(tmp_path),
        model="swe-2-high",
        devin_bin="/bin/devin",
        runner=_run,
    )
    assert text == "hello"
    assert session_id == "fresh-session"
    assert usage is None  # export in this test has no final_metrics
    assert code == 0
    cmd = captured["cmd"]
    assert cmd[0] == "/bin/devin"
    assert "-r" not in cmd
    assert "--prompt-file" in cmd
    assert "-p" in cmd
    assert "--model" in cmd and "swe-2-high" in cmd
    assert "--respect-workspace-trust" in cmd and "false" in cmd
    assert "--export" in cmd
    assert captured["env"]["DEVIN_PERMISSION_MODE"] == "bypass"
    assert captured["env"]["DEVIN_MODEL"] == "swe-2-high"


def test_call_devin_resume_passes_r_flag(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def _run(cmd: list[str], **kwargs: Any) -> Any:  # noqa: ANN401
        captured["cmd"] = list(cmd)
        captured["env"] = kwargs.get("env")
        _write_export(cmd, "existing-session")
        return _fake_completed("resumed")

    text, session_id, _usage, code = call_devin(
        "continue",
        work_dir=str(tmp_path),
        model="swe-2-high",
        devin_bin="/bin/devin",
        session_id="existing-session",
        resume=True,
        runner=_run,
    )
    assert text == "resumed"
    assert session_id == "existing-session"
    assert code == 0
    # --model and DEVIN_MODEL are forced on every call, including a resume
    # (a historical devin session created via fleet worktree runs showed an
    # empty `model` column in sessions.db without this).
    assert captured["env"]["DEVIN_MODEL"] == "swe-2-high"
    cmd = captured["cmd"]
    assert "--model" in cmd and "swe-2-high" in cmd
    assert "-r" in cmd
    assert cmd[cmd.index("-r") + 1] == "existing-session"


def test_call_devin_plan_mode_does_not_bypass(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def _run(cmd: list[str], **kwargs: Any) -> Any:  # noqa: ANN401
        captured["env"] = kwargs.get("env")
        _write_export(cmd, "plan-session")
        return _fake_completed("planned")

    call_devin(
        "plan it",
        work_dir=str(tmp_path),
        devin_bin="/bin/devin",
        mode="plan",
        runner=_run,
    )
    assert "DEVIN_PERMISSION_MODE" not in captured["env"]
    # --model is still forced (and DEVIN_MODEL still set) in plan mode.
    assert captured["env"]["DEVIN_MODEL"] == DEFAULT_MODEL


# --- call_devin: retry / backoff ---------------------------------------------


def test_call_devin_rate_limit_then_success_retries_with_cooldown(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    sleeps: list[float] = []

    def _run(cmd: list[str], **_kwargs: Any) -> Any:  # noqa: ANN401
        calls.append(list(cmd))
        _write_export(cmd, "rl-session")
        if len(calls) == 1:
            return _fake_completed(stdout="", stderr="Rate limited: slow down", returncode=1)
        return _fake_completed(stdout="all good", returncode=0)

    text, session_id, _usage, code = call_devin(
        "go",
        work_dir=str(tmp_path),
        devin_bin="/bin/devin",
        runner=_run,
        sleep=sleeps.append,
    )
    assert text == "all good"
    assert session_id == "rl-session"
    assert code == 0
    assert len(calls) == 2
    # Second call resumes the session captured from the first attempt's export.
    assert "-r" in calls[1]
    assert calls[1][calls[1].index("-r") + 1] == "rl-session"
    assert len(sleeps) == 1
    assert sleeps[0] >= 60.0  # _RATE_LIMIT_COOLDOWN_S floor


def test_call_devin_quota_exhaustion_retries_then_fails_cleanly(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    sleeps: list[float] = []

    def _run(cmd: list[str], **_kwargs: Any) -> Any:  # noqa: ANN401
        calls.append(list(cmd))
        _write_export(cmd, "quota-session")
        return _fake_completed(stdout="", stderr="Quota exhausted: no credits left", returncode=1)

    with pytest.raises(RuntimeError) as exc_info:
        call_devin(
            "go",
            work_dir=str(tmp_path),
            devin_bin="/bin/devin",
            runner=_run,
            sleep=sleeps.append,
        )
    assert "quota" in str(exc_info.value).lower()
    # _MAX_RETRIES=4 -> 5 total attempts, 4 sleeps in between.
    assert len(calls) == 5
    assert len(sleeps) == 4
    assert all(s >= 60.0 for s in sleeps)


def test_call_devin_env_override_max_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVIN_MAX_RETRIES", "1")
    calls: list[list[str]] = []

    def _run(cmd: list[str], **_kwargs: Any) -> Any:  # noqa: ANN401
        calls.append(list(cmd))
        _write_export(cmd, "s")
        return _fake_completed(stdout="", stderr="Server error: 500", returncode=1)

    with pytest.raises(RuntimeError):
        call_devin(
            "go",
            work_dir=str(tmp_path),
            devin_bin="/bin/devin",
            runner=_run,
            sleep=lambda _s: None,
        )
    assert len(calls) == 2  # 1 initial + 1 retry


def test_call_devin_non_retryable_error_fails_immediately(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def _run(cmd: list[str], **_kwargs: Any) -> Any:  # noqa: ANN401
        calls.append(list(cmd))
        _write_export(cmd, "s")
        return _fake_completed(stdout="", stderr="totally unexpected failure", returncode=1)

    with pytest.raises(RuntimeError) as exc_info:
        call_devin(
            "go",
            work_dir=str(tmp_path),
            devin_bin="/bin/devin",
            runner=_run,
            sleep=lambda _s: None,
        )
    assert len(calls) == 1
    assert "error" in str(exc_info.value).lower()


def test_call_devin_timeout_classification(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def _run(cmd: list[str], **kwargs: Any) -> Any:  # noqa: ANN401
        calls.append(list(cmd))
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))

    with pytest.raises(RuntimeError) as exc_info:
        call_devin(
            "go",
            work_dir=str(tmp_path),
            devin_bin="/bin/devin",
            timeout_s=60,
            runner=_run,
            sleep=lambda _s: None,
        )
    assert "timeout" in str(exc_info.value).lower()
    assert len(calls) == 5  # _MAX_RETRIES=4 -> 5 total attempts, all classified as timeout


# --- usage: export file, sessions.db fallback, delta harvesting -------------


def _write_sqlite_message(
    db_path: Path, session_id: str, metrics: dict[str, Any], *, node_id: int = 1
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS message_nodes ("
            "row_id INTEGER PRIMARY KEY, session_id TEXT, node_id INTEGER, chat_message TEXT)"
        )
        chat_message = json.dumps(
            {"role": "assistant", "metadata": {"metrics": metrics}}
        )
        conn.execute(
            "INSERT INTO message_nodes (session_id, node_id, chat_message) VALUES (?, ?, ?)",
            (session_id, node_id, chat_message),
        )
        conn.commit()
    finally:
        conn.close()


def test_read_export_usage_parses_final_metrics(tmp_path: Path) -> None:
    export = tmp_path / "export.json"
    export.write_text(
        json.dumps(
            {
                "session_id": "s1",
                "final_metrics": {
                    "total_prompt_tokens": 1000,
                    "total_completion_tokens": 50,
                    "total_cached_tokens": 200,
                    "total_steps": 3,
                },
            }
        ),
        encoding="utf-8",
    )
    assert _read_export_usage(str(export)) == {
        "input_tokens": 1000,
        "output_tokens": 50,
        "cache_read_tokens": 200,
        "cache_write_tokens": 0,
    }


def test_read_export_usage_missing_final_metrics_returns_none(tmp_path: Path) -> None:
    export = tmp_path / "export.json"
    export.write_text(json.dumps({"session_id": "s1"}), encoding="utf-8")
    assert _read_export_usage(str(export)) is None


def test_read_export_usage_missing_file_returns_none(tmp_path: Path) -> None:
    assert _read_export_usage(str(tmp_path / "does-not-exist.json")) is None


def test_read_sessions_db_usage_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "sessions.db"
    _write_sqlite_message(
        db_path,
        "sess-9",
        {
            "input_tokens": 500,
            "output_tokens": 30,
            "cache_read_tokens": 10,
            "cache_creation_tokens": 4,
        },
    )
    monkeypatch.setattr("agent_fleet.devin_backend.SESSIONS_DB_PATH", db_path)
    assert _read_sessions_db_usage("sess-9") == {
        "input_tokens": 500,
        "output_tokens": 30,
        "cache_read_tokens": 10,
        "cache_write_tokens": 4,
    }


def test_read_sessions_db_usage_no_db_file_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agent_fleet.devin_backend.SESSIONS_DB_PATH", tmp_path / "missing.db")
    assert _read_sessions_db_usage("whatever") is None


def test_read_sessions_db_usage_sums_across_all_turns_not_just_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: metrics per message_node are a per-turn delta, not a
    running cumulative total. A live multi-turn run showed the latest turn's
    metrics (~67k tokens) were ~37x smaller than the true session total
    (~2.5M tokens) summed across every turn — taking only the newest row
    silently under-reported live progress by orders of magnitude."""
    db_path = tmp_path / "sessions.db"
    _write_sqlite_message(
        db_path,
        "multi-turn",
        {
            "input_tokens": 100000,
            "output_tokens": 10000,
            "cache_read_tokens": 500000,
            "cache_creation_tokens": 0,
        },
        node_id=1,
    )
    _write_sqlite_message(
        db_path,
        "multi-turn",
        {
            "input_tokens": 200000,
            "output_tokens": 40000,
            "cache_read_tokens": 1500000,
            "cache_creation_tokens": 0,
        },
        node_id=2,
    )
    _write_sqlite_message(
        db_path,
        "multi-turn",
        {
            "input_tokens": 39000,
            "output_tokens": 5000,
            "cache_read_tokens": 100000,
            "cache_creation_tokens": 0,
        },
        node_id=3,
    )
    monkeypatch.setattr("agent_fleet.devin_backend.SESSIONS_DB_PATH", db_path)
    usage = _read_sessions_db_usage("multi-turn")
    assert usage == {
        "input_tokens": 339000,
        "output_tokens": 55000,
        "cache_read_tokens": 2100000,
        "cache_write_tokens": 0,
    }
    # The bug this guards against: taking only the newest row (node_id=3).
    assert usage != {
        "input_tokens": 39000,
        "output_tokens": 5000,
        "cache_read_tokens": 100000,
        "cache_write_tokens": 0,
    }


def _write_sqlite_session(db_path: Path, session_id: str, working_directory: str) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "id TEXT PRIMARY KEY, working_directory TEXT, created_at INTEGER)"
        )
        conn.execute(
            "INSERT INTO sessions (id, working_directory, created_at) VALUES (?, ?, ?)",
            (session_id, working_directory, int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()


def test_progress_poll_resolves_session_by_cwd_and_includes_it_in_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the poll loop must resolve the live session id by cwd and
    pass it to *on_progress* — previously the callback only received the
    usage dict, so callers (DevinSession/DevinBackend) logged
    ``agent_id: null`` for every mid-run ``usage.progress`` event even
    though the session id was resolvable from sessions.db."""
    from agent_fleet.devin_backend import _progress_poll_loop

    db_path = tmp_path / "sessions.db"
    work_dir = str(tmp_path / "worktree")
    _write_sqlite_session(db_path, "cwd-resolved-session", work_dir)
    _write_sqlite_message(
        db_path,
        "cwd-resolved-session",
        {
            "input_tokens": 10,
            "output_tokens": 2,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        },
        node_id=1,
    )
    _write_sqlite_message(
        db_path,
        "cwd-resolved-session",
        {
            "input_tokens": 15,
            "output_tokens": 3,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        },
        node_id=2,
    )
    monkeypatch.setattr("agent_fleet.devin_backend.SESSIONS_DB_PATH", db_path)

    calls: list[tuple[str | None, dict[str, int]]] = []
    stop_event = threading.Event()

    def _stop_after_one(session_id: str | None, usage: dict[str, int]) -> None:
        calls.append((session_id, usage))
        stop_event.set()

    _progress_poll_loop(
        work_dir=work_dir,
        export_path=str(tmp_path / "no-export-here.json"),
        session_id=None,
        started_at=time.time() - 1,
        interval=0.01,
        stop_event=stop_event,
        on_progress=_stop_after_one,
    )
    assert calls, "on_progress should fire once the session is resolved by cwd"
    session_id, usage = calls[0]
    assert session_id == "cwd-resolved-session"
    assert usage == {
        "input_tokens": 25,
        "output_tokens": 5,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }


def test_call_devin_falls_back_to_sessions_db_when_export_has_no_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "sessions.db"
    _write_sqlite_message(
        db_path,
        "db-session",
        {
            "input_tokens": 7,
            "output_tokens": 3,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        },
    )
    monkeypatch.setattr("agent_fleet.devin_backend.SESSIONS_DB_PATH", db_path)

    def _run(cmd: list[str], **_kwargs: Any) -> Any:  # noqa: ANN401
        _write_export(cmd, "db-session")  # export has a session_id but no final_metrics
        return _fake_completed("hi")

    _text, session_id, usage, _code = call_devin(
        "go", work_dir=str(tmp_path), devin_bin="/bin/devin", runner=_run
    )
    assert session_id == "db-session"
    assert usage == {
        "input_tokens": 7,
        "output_tokens": 3,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }


def test_harvest_devin_usage_computes_delta_across_resumed_turns() -> None:
    """Devin's own counters are cumulative for the whole session (verified live:
    a second turn on the same session roughly doubled the totals), so the
    harvester must diff, not pass the raw cumulative total through."""
    import agent_fleet.devin_backend as devin_backend_module

    devin_backend_module._last_session_usage.pop("delta-session", None)
    first = _harvest_devin_usage(
        cumulative={
            "input_tokens": 100,
            "output_tokens": 10,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        },
        session_id="delta-session",
        phase=None,
        model="swe-2-high",
        duration_s=1.0,
    )
    assert first == {
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    second = _harvest_devin_usage(
        cumulative={
            "input_tokens": 260,
            "output_tokens": 25,
            "cache_read_tokens": 5,
            "cache_write_tokens": 0,
        },
        session_id="delta-session",
        phase=None,
        model="swe-2-high",
        duration_s=1.0,
    )
    assert second == {
        "input_tokens": 160,
        "output_tokens": 15,
        "cache_read_tokens": 5,
        "cache_write_tokens": 0,
    }
    devin_backend_module._last_session_usage.pop("delta-session", None)


def test_harvest_devin_usage_none_cumulative_returns_none() -> None:
    assert (
        _harvest_devin_usage(
            cumulative=None, session_id="x", phase=None, model="m", duration_s=0.0
        )
        is None
    )


# --- progress callback --------------------------------------------------------


def test_call_devin_progress_callback_invoked_during_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No sessions.db in play here (export-file path only) — point at a
    # guaranteed-missing path so a real dev machine's sessions.db can never
    # leak a session id into this test.
    monkeypatch.setattr("agent_fleet.devin_backend.SESSIONS_DB_PATH", tmp_path / "missing.db")
    progress_calls: list[tuple[str | None, dict[str, int]]] = []

    def _run(cmd: list[str], **_kwargs: Any) -> Any:  # noqa: ANN401
        idx = cmd.index("--export")
        path = Path(cmd[idx + 1])
        path.write_text(
            json.dumps(
                {
                    "session_id": "prog-session",
                    "final_metrics": {
                        "total_prompt_tokens": 100,
                        "total_completion_tokens": 20,
                        "total_cached_tokens": 5,
                    },
                }
            ),
            encoding="utf-8",
        )
        # Give the background poll thread time to fire at least once before
        # the (fake) subprocess "finishes".
        time.sleep(0.15)
        return _fake_completed("done")

    call_devin(
        "go",
        work_dir=str(tmp_path),
        devin_bin="/bin/devin",
        runner=_run,
        on_progress=lambda session_id, usage: progress_calls.append((session_id, usage)),
        progress_interval_s=0.03,
    )
    assert progress_calls, "on_progress should fire at least once while the call is in flight"
    assert progress_calls[0][1]["input_tokens"] == 100


def test_call_devin_no_progress_thread_when_on_progress_none(tmp_path: Path) -> None:
    """Default (on_progress=None) path must not spin up a poll thread at all."""
    import threading

    threads_before = threading.active_count()

    def _run(cmd: list[str], **_kwargs: Any) -> Any:  # noqa: ANN401
        _write_export(cmd, "no-progress-session")
        assert threading.active_count() == threads_before
        return _fake_completed("done")

    call_devin("go", work_dir=str(tmp_path), devin_bin="/bin/devin", runner=_run)


# --- DevinSession / DevinBackend ----------------------------------------------


def test_session_first_send_fresh_second_resumes(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def _run(cmd: list[str], **_kwargs: Any) -> Any:  # noqa: ANN401
        calls.append(list(cmd))
        _write_export(cmd, "sess-123")
        return _fake_completed("turn-response")

    import agent_fleet.devin_backend as devin_backend_module

    session = DevinSession(devin_bin="/bin/devin", model="swe-2-high", cwd=tmp_path)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(devin_backend_module.subprocess, "run", _run)
        result1 = session.send("first", max_tokens=0, timeout_s=60)
        result2 = session.send("second", max_tokens=0, timeout_s=60)

    assert result1.exit_code == 0
    assert result2.exit_code == 0
    assert session.agent_id == "sess-123"
    assert "-r" not in calls[0]
    assert "-r" in calls[1]
    assert calls[1][calls[1].index("-r") + 1] == "sess-123"


def test_session_constructor_session_id_resumes_from_first_send(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def _run(cmd: list[str], **_kwargs: Any) -> Any:  # noqa: ANN401
        calls.append(list(cmd))
        _write_export(cmd, "preexisting")
        return _fake_completed("ok")

    import agent_fleet.devin_backend as devin_backend_module

    session = DevinSession(
        devin_bin="/bin/devin", model="swe-2-high", cwd=tmp_path, session_id="preexisting"
    )
    assert session.agent_id == "preexisting"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(devin_backend_module.subprocess, "run", _run)
        session.send("continue", max_tokens=0, timeout_s=60)

    assert "-r" in calls[0]
    assert calls[0][calls[0].index("-r") + 1] == "preexisting"


def test_create_session_error_when_auth_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "agent_fleet.devin_backend.check_devin_auth",
        lambda: (False, "devin binary not found", "install the Devin CLI"),
    )
    backend = DevinBackend(devin_bin="/bin/devin")
    session = backend.create_session(persona_name="coder", cwd=tmp_path)
    assert isinstance(session, _DevinErrorSession)
    result = session.send("hi", max_tokens=0, timeout_s=60)
    assert result.exit_code == 1
    assert "devin binary not found" in result.stderr


def test_create_session_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "agent_fleet.devin_backend.check_devin_auth", lambda: (True, "authenticated", "")
    )
    backend = DevinBackend(devin_bin="/bin/devin", model="swe-2-high")
    session = backend.create_session(persona_name="coder", cwd=tmp_path)
    assert isinstance(session, DevinSession)


def test_run_returns_error_when_auth_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "agent_fleet.devin_backend.check_devin_auth",
        lambda: (False, "credentials missing", "run `devin auth login`"),
    )
    backend = DevinBackend(devin_bin="/bin/devin")
    result = backend.run("hi", max_tokens=0, timeout_s=60, cwd=tmp_path)
    assert isinstance(result, DevinLLMResult)
    assert result.exit_code == 1
    assert "devin auth login" in result.stderr


def test_run_success_and_scope_note(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "agent_fleet.devin_backend.check_devin_auth", lambda: (True, "authenticated", "")
    )
    captured: dict[str, Any] = {}

    def _run(cmd: list[str], **_kwargs: Any) -> Any:  # noqa: ANN401
        prompt_file = Path(cmd[cmd.index("--prompt-file") + 1])
        captured["prompt"] = prompt_file.read_text(encoding="utf-8")
        _write_export(cmd, "run-session")
        return _fake_completed("done")

    import agent_fleet.devin_backend as devin_backend_module

    backend = DevinBackend(devin_bin="/bin/devin")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(devin_backend_module.subprocess, "run", _run)
        result = backend.run(
            "do the thing",
            max_tokens=0,
            timeout_s=60,
            cwd=tmp_path,
            allowed_tools=["path:src/"],
        )
    assert result.exit_code == 0
    assert result.stdout == "done"
    assert result.agent_id == "run-session"
    assert "only modify files under these prefixes: src/" in captured["prompt"]


def test_run_populates_usage_from_export(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "agent_fleet.devin_backend.check_devin_auth", lambda: (True, "authenticated", "")
    )
    import agent_fleet.devin_backend as devin_backend_module

    devin_backend_module._last_session_usage.pop("usage-session", None)

    def _run(cmd: list[str], **_kwargs: Any) -> Any:  # noqa: ANN401
        idx = cmd.index("--export")
        path = Path(cmd[idx + 1])
        path.write_text(
            json.dumps(
                {
                    "session_id": "usage-session",
                    "final_metrics": {
                        "total_prompt_tokens": 42,
                        "total_completion_tokens": 8,
                        "total_cached_tokens": 1,
                    },
                }
            ),
            encoding="utf-8",
        )
        return _fake_completed("done")

    backend = DevinBackend(devin_bin="/bin/devin")
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(devin_backend_module.subprocess, "run", _run)
            result = backend.run("do it", max_tokens=0, timeout_s=60, cwd=tmp_path)
        assert result.usage == {
            "input_tokens": 42,
            "output_tokens": 8,
            "cache_read_tokens": 1,
            "cache_write_tokens": 0,
        }
    finally:
        devin_backend_module._last_session_usage.pop("usage-session", None)


def test_backend_run_progress_event_includes_resolved_session_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: ``DevinBackend.run``'s ``_on_progress`` closure used to
    build the ``usage.progress`` event without a session id at all (always
    ``agent_id: null`` mid-run), even once ``_progress_poll_loop`` had
    resolved the live session by cwd. The closure must use the session id
    ``call_devin`` passes into ``on_progress``, not a value only known after
    the call returns."""
    from agent_fleet.observability.context import bind_run
    from agent_fleet.observability.events import RunContext

    monkeypatch.setattr(
        "agent_fleet.devin_backend.check_devin_auth", lambda: (True, "authenticated", "")
    )

    emitted: list[tuple[str, dict[str, Any]]] = []

    class _FakeRunLog:
        def emit(self, event: str, *, data: dict[str, Any] | None = None) -> None:
            emitted.append((event, data or {}))

    def _fake_call_devin(
        _prompt: str, **kwargs: Any  # noqa: ANN401
    ) -> tuple[str, str | None, dict[str, int] | None, int]:
        on_progress = kwargs.get("on_progress")
        if on_progress is not None:
            on_progress(
                "resolved-session",
                {
                    "input_tokens": 5,
                    "output_tokens": 1,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                },
            )
        return "done", "resolved-session", None, 0

    monkeypatch.setattr("agent_fleet.devin_backend.call_devin", _fake_call_devin)

    backend = DevinBackend(devin_bin="/bin/devin")
    with bind_run(_FakeRunLog(), RunContext(run_id="test-run")):
        backend.run("do it", max_tokens=0, timeout_s=60, cwd=tmp_path)

    progress_events = [data for event, data in emitted if event == "usage.progress"]
    assert progress_events, "usage.progress should have been emitted"
    assert progress_events[0]["agent_id"] == "resolved-session"


def test_session_send_progress_event_includes_resolved_session_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Same regression as above, for ``DevinSession.send``: the closure
    previously logged ``self.agent_id`` (only set *after* ``call_devin``
    returns) instead of the session id ``on_progress`` was actually called
    with, so a fresh session's mid-run events always had ``agent_id: null``."""
    from agent_fleet.observability.context import bind_run
    from agent_fleet.observability.events import RunContext

    emitted: list[tuple[str, dict[str, Any]]] = []

    class _FakeRunLog:
        def emit(self, event: str, *, data: dict[str, Any] | None = None) -> None:
            emitted.append((event, data or {}))

    def _fake_call_devin(
        _prompt: str, **kwargs: Any  # noqa: ANN401
    ) -> tuple[str, str | None, dict[str, int] | None, int]:
        on_progress = kwargs.get("on_progress")
        if on_progress is not None:
            on_progress(
                "live-session",
                {
                    "input_tokens": 9,
                    "output_tokens": 2,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                },
            )
        return "done", "live-session", None, 0

    monkeypatch.setattr("agent_fleet.devin_backend.call_devin", _fake_call_devin)

    session = DevinSession(devin_bin="/bin/devin", model="swe-2-high", cwd=tmp_path)
    with bind_run(_FakeRunLog(), RunContext(run_id="test-run")):
        # self.agent_id is still None here (fresh session, first send) —
        # the emitted event must use the on_progress-supplied id instead.
        assert session.agent_id is None
        session.send("go", max_tokens=0, timeout_s=60)

    progress_events = [data for event, data in emitted if event == "usage.progress"]
    assert progress_events, "usage.progress should have been emitted"
    assert progress_events[0]["agent_id"] == "live-session"


# --- import isolation ---------------------------------------------------------


def test_import_isolation_devin_does_not_import_others() -> None:
    import sys

    _backend_mods = (
        "agent_fleet.cursor_backend",
        "agent_fleet.kimi_backend",
        "agent_fleet.openrouter_backend",
        "agent_fleet.grok_backend",
        "agent_fleet.cmd_backend",
        "agent_fleet.devin_backend",
    )
    saved = {m: sys.modules.get(m) for m in _backend_mods}
    for m in _backend_mods:
        sys.modules.pop(m, None)
    try:
        from agent_fleet.backends import make_backend
        from agent_fleet.config import FleetConfig

        cfg = FleetConfig(default_backend="devin", default_model=None)
        make_backend(cfg)

        assert "agent_fleet.devin_backend" in sys.modules
        assert "agent_fleet.cursor_backend" not in sys.modules
        assert "agent_fleet.kimi_backend" not in sys.modules
        assert "agent_fleet.openrouter_backend" not in sys.modules
        assert "agent_fleet.grok_backend" not in sys.modules
        assert "agent_fleet.cmd_backend" not in sys.modules
    finally:
        for m, mod in saved.items():
            if mod is not None:
                sys.modules[m] = mod
            else:
                sys.modules.pop(m, None)


# --- CLI help text lists devin (regression: hardcoded backend lists) ---------


def test_registered_backend_names_includes_devin() -> None:
    from agent_fleet.backends import registered_backend_names

    names = registered_backend_names()
    assert "devin" in names
    assert names == tuple(sorted(names))


def test_cli_run_help_lists_devin_backend() -> None:
    import subprocess
    import sys as _sys

    result = subprocess.run(
        [_sys.executable, "-m", "agent_fleet.cli", "run", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "devin" in result.stdout


def test_cli_doctor_help_lists_devin_backend() -> None:
    import subprocess
    import sys as _sys

    result = subprocess.run(
        [_sys.executable, "-m", "agent_fleet.cli", "doctor", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "devin" in result.stdout


def test_cli_run_accepts_devin_backend_flag_dry_run(tmp_path: Path) -> None:
    """``--backend devin`` must not be rejected by argparse (no stale choices=)."""
    import subprocess
    import sys as _sys

    result = subprocess.run(
        [
            _sys.executable,
            "-m",
            "agent_fleet.cli",
            "run",
            "--backend",
            "devin",
            "--dry-run",
            "--workspace",
            str(tmp_path),
            "hello",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "invalid choice" not in result.stderr.lower()
