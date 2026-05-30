"""Tests for structured fleet observability."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

from agent_fleet.observability.context import bind_phase, bind_run, get_run_log
from agent_fleet.observability.efficiency import changed_lines
from agent_fleet.observability.events import FleetEvent, RunContext
from agent_fleet.observability.log import RunLog
from agent_fleet.observability.sinks import JsonlFileSink, MemoryRingSink

if TYPE_CHECKING:
    from pathlib import Path


def test_fleet_event_roundtrip() -> None:
    event = FleetEvent.now(
        run_id="abc123",
        event="phase.start",
        phase="PLAN",
        data={"items": 2},
    )
    payload = json.loads(event.to_json())
    assert payload["run_id"] == "abc123"
    assert payload["event"] == "phase.start"
    assert payload["phase"] == "PLAN"
    assert payload["data"]["items"] == 2


def test_run_log_writes_jsonl(tmp_path: Path) -> None:
    run_log = RunLog.create(
        run_id="testrun1",
        issue_number=42,
        persona="frontend",
        runs_dir=tmp_path,
        include_memory_ring=False,
    )
    with bind_run(run_log, run_log.context):
        run_log.emit("run.start", data={"title": "Example"})
        run_log.memory(available_ram_gb=32.0, playwright_mcp_processes=1)

    path = tmp_path / "testrun1.jsonl"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["event"] == "run.start"
    assert first["issue_number"] == 42


def test_bind_run_context_var() -> None:
    run_log = RunLog.create(
        run_id="ctx1",
        include_memory_ring=False,
    )
    assert get_run_log() is None
    with bind_run(run_log, run_log.context):
        assert get_run_log() is run_log


def test_memory_ring_sink() -> None:
    sink = MemoryRingSink(max_events=2)
    sink.emit(FleetEvent.now(run_id="r1", event="a"))
    sink.emit(FleetEvent.now(run_id="r1", event="b"))
    sink.emit(FleetEvent.now(run_id="r1", event="c"))
    assert [event.event for event in sink.events] == ["b", "c"]


def test_jsonl_sink_creates_parent_dir(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "run.jsonl"
    sink = JsonlFileSink(path)
    sink.emit(FleetEvent.now(run_id="nested", event="run.start"))
    assert path.is_file()


def test_run_context_fields() -> None:
    ctx = RunContext(run_id="x", issue_number=1, persona="backend", visual_audit=True)
    assert ctx.visual_audit is True
    assert ctx.issue_number == 1


def test_llm_usage_phase_attribution() -> None:
    run_log = RunLog.create(run_id="phase-test", include_memory_ring=False)
    with bind_run(run_log, run_log.context):
        with bind_phase("execute"):
            run_log.llm_usage(
                phase=None,
                model="m",
                duration_s=0.1,
                input_tokens=10,
                output_tokens=5,
            )
        assert "execute" in run_log._usage_by_phase
        assert "unknown" not in run_log._usage_by_phase

        run_log.llm_usage(phase=None, model="m", duration_s=0.1)
        assert "unknown" in run_log._usage_by_phase


def test_usage_rollup_efficiency_fields() -> None:
    run_log = RunLog.create(run_id="eff-test", include_memory_ring=False)
    with bind_run(run_log, run_log.context):
        run_log.llm_usage(
            phase="execute",
            model="m",
            duration_s=0.1,
            input_tokens=60,
            output_tokens=40,
        )
    # 100 total tokens, 50 changed lines → tokens_per_changed_line == 2
    payload = run_log._usage_rollup_payload(changed_lines=50)
    assert payload is not None
    assert payload["changed_lines"] == 50
    assert payload["tokens_per_changed_line"] == 2

    # changed_lines=0 fallback: tokens_per_changed_line == total_tokens (denominator 1)
    payload_zero = run_log._usage_rollup_payload(changed_lines=0)
    assert payload_zero is not None
    assert payload_zero["changed_lines"] == 0
    assert payload_zero["tokens_per_changed_line"] == payload_zero["totals"]["total_tokens"]


def test_run_end_emits_efficiency_headline(tmp_path: Path) -> None:
    from agent_fleet.observability.sinks import MemoryRingSink

    ring = MemoryRingSink(max_events=50)
    run_log = RunLog.create(
        run_id="eff-headline",
        runs_dir=tmp_path,
        include_memory_ring=False,
    )
    run_log._sinks.append(ring)

    with bind_run(run_log, run_log.context):
        run_log.llm_usage(
            phase="execute",
            model="m",
            duration_s=0.1,
            input_tokens=80,
            output_tokens=20,
        )
        run_log.run_end(outcome="completed", changed_lines=10)

    headline_events = [e for e in ring.events if e.event == "efficiency.headline"]
    assert len(headline_events) == 1
    d = headline_events[0].data
    assert d is not None
    assert d["run_id"] == "eff-headline"
    assert d["total_tokens"] == 100
    assert d["changed_lines"] == 10
    assert d["tokens_per_changed_line"] == 10
    # headline string is greppable
    assert "EFFICIENCY" in d["headline"]
    assert "run=eff-headline" in d["headline"]
    assert "total_tokens=100" in d["headline"]
    assert "changed_lines=10" in d["headline"]
    assert "tokens_per_changed_line=10" in d["headline"]


def test_run_end_uses_passed_changed_lines_when_rollup_stale(tmp_path: Path) -> None:
    """Dispatcher calls task_usage_rollup(changed_lines=0) before run_end does.
    run_end must use the passed changed_lines, not the stale rollup value."""
    from agent_fleet.observability.sinks import MemoryRingSink

    ring = MemoryRingSink(max_events=50)
    run_log = RunLog.create(
        run_id="dispatcher-cl-test",
        runs_dir=tmp_path,
        include_memory_ring=False,
    )
    run_log._sinks.append(ring)

    with bind_run(run_log, run_log.context):
        run_log.llm_usage(
            phase="execute",
            model="m",
            duration_s=0.1,
            input_tokens=100,
            output_tokens=100,
        )
        # Simulates dispatcher _peek_usage_rollup: fires idempotency guard with changed_lines=0.
        run_log.task_usage_rollup(task_id=1, changed_lines=0)
        # run_end called with the real changed_lines after commit.
        run_log.run_end(outcome="completed", changed_lines=40)

    headline_events = [e for e in ring.events if e.event == "efficiency.headline"]
    assert len(headline_events) == 1
    d = headline_events[0].data
    assert d is not None
    assert d["changed_lines"] == 40
    assert d["tokens_per_changed_line"] == round(200 / 40)


def test_dispatcher_rollup_carries_changed_lines(tmp_path: Path) -> None:
    """read_observed_total_tokens is the dispatcher path's only task_usage_rollup
    emit (it never calls run_end). The emitted llm.usage.task_rollup must carry
    the real changed_lines so efficiency_report shows a non-zero denominator."""
    from agent_fleet.dispatcher_task import read_observed_total_tokens
    from agent_fleet.observability.sinks import MemoryRingSink

    ring = MemoryRingSink(max_events=50)
    run_log = RunLog.create(
        run_id="dispatch-rollup",
        runs_dir=tmp_path,
        include_memory_ring=False,
    )
    run_log._sinks.append(ring)

    with bind_run(run_log, run_log.context):
        run_log.llm_usage(
            phase="execute",
            model="m",
            duration_s=0.1,
            input_tokens=300,
            output_tokens=300,
        )
        total = read_observed_total_tokens(task_index=7, changed_lines=60)

    assert total == 600
    rollups = [e for e in ring.events if e.event == "llm.usage.task_rollup"]
    assert len(rollups) == 1
    data = rollups[0].data
    assert data is not None
    assert data["changed_lines"] == 60
    assert data["tokens_per_changed_line"] == round(600 / 60)


def test_efficiency_report_synthetic(tmp_path: Path) -> None:
    # Run with changed_lines present
    run_a = tmp_path / "run-abc.jsonl"
    run_a.write_text(
        json.dumps(
            {
                "event": "llm.usage.task_rollup",
                "data": {
                    "totals": {"input_tokens": 600, "output_tokens": 400, "total_tokens": 1000},
                    "changed_lines": 50,
                    "tokens_per_changed_line": 20,
                    "by_phase": {"plan": {"total_tokens": 200}, "execute": {"total_tokens": 800}},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    # Run without changed_lines (pre-U2 log)
    run_b = tmp_path / "run-xyz.jsonl"
    run_b.write_text(
        json.dumps(
            {
                "event": "llm.usage.task_rollup",
                "data": {
                    "totals": {"input_tokens": 300, "output_tokens": 200, "total_tokens": 500},
                    "by_phase": {"plan": {"total_tokens": 100}, "execute": {"total_tokens": 400}},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    from pathlib import Path as _Path

    repo_root = _Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "scripts/efficiency_report.py",
            "--runs-dir",
            str(tmp_path),
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
    )
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    assert len(rows) == 2
    by_id = {r["run_id"]: r for r in rows}
    assert by_id["run-abc"]["total_tokens"] == 1000
    assert by_id["run-abc"]["changed_lines"] == 50
    assert by_id["run-xyz"]["total_tokens"] == 500
    # No changed_lines key in log → defaults to 0
    assert by_id["run-xyz"]["changed_lines"] == 0
    # Sorted by total_tokens descending: run-abc first
    assert rows[0]["run_id"] == "run-abc"


def test_changed_lines_non_git_dir(tmp_path: Path) -> None:
    # Non-git directory should return 0.
    assert changed_lines(tmp_path) == 0


def test_changed_lines_none() -> None:
    assert changed_lines(None) == 0


def test_changed_lines_git_repo(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    f = tmp_path / "hello.txt"
    f.write_text("line1\nline2\nline3\n")
    subprocess.run(["git", "add", "hello.txt"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    # Uncommitted working-tree edits count vs HEAD: 2 deletions + 2 additions = 4.
    f.write_text("line1\nchanged2\nchanged3\n")
    assert changed_lines(tmp_path) == 4

    # After committing, the tree is clean so we fall back to the last commit
    # (HEAD~1..HEAD), which is the same 2 deletions + 2 additions = 4.
    subprocess.run(["git", "add", "hello.txt"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "edit"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    assert changed_lines(tmp_path) == 4


def test_changed_lines_uncommitted_with_untracked(tmp_path: Path) -> None:
    """Dispatcher path: work is in the working tree (not yet committed) when the
    rollup fires. Modified tracked files plus new untracked files must both count."""
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    base = tmp_path / "base.txt"
    base.write_text("a\nb\nc\n")
    subprocess.run(["git", "add", "base.txt"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "base"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    # Uncommitted: modify a tracked file (1 deletion + 1 addition) and add a new
    # untracked 3-line file. Nothing is committed, mirroring the dispatcher state.
    base.write_text("a\nB\nc\n")
    (tmp_path / "new.py").write_text("x = 1\ny = 2\nz = 3\n")
    assert changed_lines(tmp_path) == 2 + 3


def test_changed_lines_single_commit_returns_zero(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    (tmp_path / "a.txt").write_text("hello\n")
    subprocess.run(["git", "add", "a.txt"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "only commit"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    # HEAD~1 doesn't exist → git diff exits non-zero → returns 0.
    assert changed_lines(tmp_path) == 0
