"""End-to-end tests for the agent-fleet CLI surfaces: doctor, runs, watch, run --dry-run."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from agent_fleet.cli import main
from agent_fleet.observability.log import RunLog

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _seed_run(tmp_path: Path) -> None:
    rl = RunLog.create(run_id="run-test-0001", runs_dir=tmp_path)
    rl.run_start(title="Audit the API")
    rl.emit("program.phase", data={"title": "discover"})
    rl.emit("program.agent.start", data={"idx": 0, "persona": "backend", "goal": "scan"})
    rl.emit("program.agent.done", data={"idx": 0, "status": "completed", "tokens": 1500})
    rl.run_end(outcome="completed", changed_lines=10)


# ---------------------------------------------------------------------------
# (a) doctor
# ---------------------------------------------------------------------------


def test_doctor_returns_0_and_renders_header(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "k")
    code = main(["doctor"])
    assert code == 0
    out = capsys.readouterr().out
    assert "agent-fleet doctor" in out


def test_doctor_json_returns_list_of_checks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "k")
    code = main(["doctor", "--json"])
    assert code == 0
    out = capsys.readouterr().out
    rows = json.loads(out)
    assert isinstance(rows, list)
    assert len(rows) > 0
    for row in rows:
        assert "name" in row
        assert "status" in row


# ---------------------------------------------------------------------------
# (b) runs
# ---------------------------------------------------------------------------


def test_runs_table_contains_run_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path))
    _seed_run(tmp_path)
    code = main(["runs"])
    assert code == 0
    out = capsys.readouterr().out
    assert "run-test-0001" in out
    assert "RUN ID" in out


def test_runs_json_has_run_id_and_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path))
    _seed_run(tmp_path)
    code = main(["runs", "--json"])
    assert code == 0
    out = capsys.readouterr().out
    rows = json.loads(out)
    assert isinstance(rows, list)
    assert len(rows) >= 1
    first = rows[0]
    assert first["run_id"] == "run-test-0001"
    assert first["status"] == "completed"


# ---------------------------------------------------------------------------
# (c) watch --once / --json
# ---------------------------------------------------------------------------


def test_watch_once_shows_run_phase_and_tokens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path))
    _seed_run(tmp_path)
    code = main(["watch", "run-test", "--once"])
    assert code == 0
    out = capsys.readouterr().out
    assert "run-test-0001" in out
    assert "discover" in out
    assert "1500" in out


def test_watch_latest_json_returns_folded_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path))
    _seed_run(tmp_path)
    code = main(["watch", "latest", "--json"])
    assert code == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["run_id"] == "run-test-0001"
    assert payload["status"] == "completed"
    assert payload["tokens"] == 1500
    assert isinstance(payload["agents"], list)
    assert len(payload["agents"]) > 0
    assert payload["agents"][0]["persona"] == "backend"


# ---------------------------------------------------------------------------
# (d) watch not-found
# ---------------------------------------------------------------------------


def test_watch_missing_run_returns_1_and_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path))
    code = main(["watch", "does-not-exist", "--once"])
    assert code == 1
    err = capsys.readouterr().err
    assert "no run matching" in err


# ---------------------------------------------------------------------------
# (e) run --dry-run short-circuits before backend env gate
# ---------------------------------------------------------------------------


def test_dry_run_short_circuits_before_backend_env(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    code = main(["run", "ship a thing", "--pipeline", "simple", "--dry-run"])
    assert code == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["dry_run"] is True
    assert payload["goal"] == "ship a thing"
    assert payload["pipeline"] == "simple"
