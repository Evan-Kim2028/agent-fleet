"""Tests for scripts/outcome_scorecard.py."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(tmp_path: Path, extra_args: list[str] | None = None) -> dict:
    cmd = [
        "uv",
        "run",
        "python",
        "scripts/outcome_scorecard.py",
        "--runs-dir",
        str(tmp_path),
        "--json",
    ]
    if extra_args:
        cmd.extend(extra_args)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _write(path: Path, events: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(e) for e in events) + "\n",
        encoding="utf-8",
    )


def test_real_completed(tmp_path: Path) -> None:
    _write(
        tmp_path / "real-completed.jsonl",
        [
            {
                "ts": "2026-05-29T00:00:00+00:00",
                "run_id": "real-completed",
                "event": "fleet.task.start",
                "level": "info",
                "persona": "lakestore",
                "data": {
                    "task_index": 0,
                    "persona": "lakestore",
                    "goal": "fix snapshot bootstrap bug",
                    "has_handoff": False,
                },
            },
            {
                "ts": "2026-05-29T00:01:00+00:00",
                "run_id": "real-completed",
                "event": "llm.usage",
                "level": "info",
                "phase": "execute",
                "data": {
                    "model": "composer-2.5",
                    "agent_id": "agent-abc",
                    "duration_s": 60.0,
                    "input_tokens": 50000,
                    "output_tokens": 5000,
                    "cache_read_tokens": 40000,
                    "cache_write_tokens": 0,
                    "total_tokens": 95000,
                },
            },
            {
                "ts": "2026-05-29T00:02:00+00:00",
                "run_id": "real-completed",
                "event": "fleet.task.complete",
                "level": "info",
                "persona": "lakestore",
                "data": {
                    "task_index": 0,
                    "persona": "lakestore",
                    "status": "completed",
                    "duration_seconds": 120.0,
                    "outcome_metrics": {
                        "status": "completed",
                        "verify_attempts": 1,
                        "fix_attempts": 0,
                        "repo_key": "silphcoanalytics",
                        "issue_number": 42,
                        "duration_seconds": 120.0,
                        "changed_files_count": 3,
                        "verify_failure": None,
                    },
                    "error": None,
                    "stderr_snippet": None,
                },
            },
        ],
    )

    out = _run(tmp_path)
    runs_by_id = {r["run_id"]: r for r in out["runs"]}
    r = runs_by_id["real-completed"]

    assert r["kind"] == "real"
    assert r["success"] is True
    assert r["status"] == "completed"
    assert r["model"] == "composer-2.5"
    assert r["persona"] == "lakestore"
    assert r["goal"] == "fix snapshot bootstrap bug"
    assert r["tokens_total"] == 95000
    assert r["fix_attempts"] == 0
    assert r["verify_attempts"] == 1
    assert r["repo_key"] == "silphcoanalytics"
    assert r["issue_number"] == 42
    assert r["changed_files_count"] == 3
    assert r["pipeline"] == "simple"


def test_real_verify_failed(tmp_path: Path) -> None:
    _write(
        tmp_path / "real-verify-failed.jsonl",
        [
            {
                "ts": "2026-05-29T00:00:00+00:00",
                "run_id": "real-verify-failed",
                "event": "fleet.task.start",
                "level": "info",
                "persona": "coder",
                "data": {
                    "task_index": 0,
                    "persona": "coder",
                    "goal": "refactor dispatch loop",
                    "has_handoff": False,
                },
            },
            {
                "ts": "2026-05-29T00:01:00+00:00",
                "run_id": "real-verify-failed",
                "event": "llm.usage",
                "level": "info",
                "phase": "execute",
                "data": {
                    "model": "composer-2.5",
                    "agent_id": "agent-xyz",
                    "duration_s": 80.0,
                    "input_tokens": 100000,
                    "output_tokens": 8000,
                    "cache_read_tokens": 90000,
                    "cache_write_tokens": 0,
                    "total_tokens": 198000,
                },
            },
            {
                "ts": "2026-05-29T00:02:00+00:00",
                "run_id": "real-verify-failed",
                "event": "llm.usage",
                "level": "info",
                "phase": "fix",
                "data": {
                    "model": "composer-2.5",
                    "agent_id": "agent-xyz",
                    "duration_s": 30.0,
                    "input_tokens": 40000,
                    "output_tokens": 3000,
                    "cache_read_tokens": 35000,
                    "cache_write_tokens": 0,
                    "total_tokens": 78000,
                },
            },
            {
                "ts": "2026-05-29T00:03:00+00:00",
                "run_id": "real-verify-failed",
                "event": "fleet.task.complete",
                "level": "info",
                "persona": "coder",
                "data": {
                    "task_index": 0,
                    "persona": "coder",
                    "status": "verify_failed",
                    "duration_seconds": 200.0,
                    "outcome_metrics": {
                        "status": "verify_failed",
                        "verify_attempts": 2,
                        "fix_attempts": 1,
                        "repo_key": "agent-fleet",
                        "issue_number": 0,
                        "duration_seconds": 200.0,
                        "changed_files_count": 0,
                        "verify_failure": True,
                    },
                    "error": None,
                    "stderr_snippet": None,
                },
            },
        ],
    )

    out = _run(tmp_path)
    runs_by_id = {r["run_id"]: r for r in out["runs"]}
    r = runs_by_id["real-verify-failed"]

    assert r["kind"] == "real"
    assert r["success"] is False
    assert r["status"] == "verify_failed"
    assert r["fix_attempts"] == 1
    assert r["verify_attempts"] == 2
    assert r["verify_failure"] is True
    assert r["tokens_total"] == 276000
    assert r["fix_phase_tokens"] == 78000


def test_synthetic_excluded_from_summary(tmp_path: Path) -> None:
    _write(
        tmp_path / "synth-dispatch.jsonl",
        [
            {
                "ts": "2026-05-29T00:00:00+00:00",
                "run_id": "synth-dispatch",
                "event": "fleet.task.start",
                "level": "info",
                "persona": "coder",
                "data": {
                    "task_index": 0,
                    "persona": "coder",
                    "goal": "legacy",
                    "has_handoff": False,
                },
            },
            {
                "ts": "2026-05-29T00:00:01+00:00",
                "run_id": "synth-dispatch",
                "event": "llm.usage",
                "level": "info",
                "phase": "execute",
                "data": {
                    "model": "m",
                    "agent_id": None,
                    "duration_s": 0.1,
                    "input_tokens": 0,
                    "output_tokens": 7,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "total_tokens": 7,
                },
            },
            {
                "ts": "2026-05-29T00:00:02+00:00",
                "run_id": "synth-dispatch",
                "event": "fleet.task.complete",
                "level": "info",
                "persona": "coder",
                "data": {
                    "task_index": 0,
                    "persona": "coder",
                    "status": "completed",
                    "duration_seconds": 0.09,
                    "outcome_metrics": {
                        "status": "completed",
                        "verify_attempts": 0,
                        "fix_attempts": 0,
                        "repo_key": "test_dispatch_x",
                        "issue_number": 0,
                        "duration_seconds": 0.09,
                        "changed_files_count": 0,
                    },
                    "error": None,
                    "stderr_snippet": None,
                },
            },
        ],
    )

    out = _run(tmp_path)
    runs_by_id = {r["run_id"]: r for r in out["runs"]}
    r = runs_by_id["synth-dispatch"]

    assert r["kind"] == "synthetic"
    # Synthetic excluded from default summary
    assert out["summary"]["total"] == 0


def test_real_and_synthetic_summary_separation(tmp_path: Path) -> None:
    # Real completed run
    _write(
        tmp_path / "r1.jsonl",
        [
            {
                "run_id": "r1",
                "event": "fleet.task.start",
                "data": {"persona": "backend", "goal": "g1", "has_handoff": False, "task_index": 0},
            },
            {
                "run_id": "r1",
                "event": "llm.usage",
                "phase": "execute",
                "data": {
                    "model": "composer-2.5",
                    "total_tokens": 50000,
                    "input_tokens": 30000,
                    "output_tokens": 5000,
                    "cache_read_tokens": 15000,
                    "cache_write_tokens": 0,
                    "duration_s": 30.0,
                    "agent_id": "a1",
                },
            },
            {
                "run_id": "r1",
                "event": "fleet.task.complete",
                "data": {
                    "status": "completed",
                    "task_index": 0,
                    "persona": "backend",
                    "duration_seconds": 60.0,
                    "outcome_metrics": {
                        "status": "completed",
                        "fix_attempts": 0,
                        "verify_attempts": 0,
                        "repo_key": "silphcoanalytics",
                        "issue_number": 0,
                        "duration_seconds": 60.0,
                        "changed_files_count": 2,
                    },
                    "error": None,
                    "stderr_snippet": None,
                },
            },
        ],
    )
    # Synthetic run
    _write(
        tmp_path / "s1.jsonl",
        [
            {
                "run_id": "s1",
                "event": "fleet.task.start",
                "data": {
                    "persona": "coder",
                    "goal": "synth",
                    "has_handoff": False,
                    "task_index": 0,
                },
            },
            {
                "run_id": "s1",
                "event": "llm.usage",
                "phase": "execute",
                "data": {
                    "model": "m",
                    "total_tokens": 5,
                    "input_tokens": 0,
                    "output_tokens": 5,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "duration_s": 0.01,
                    "agent_id": None,
                },
            },
            {
                "run_id": "s1",
                "event": "fleet.task.complete",
                "data": {
                    "status": "completed",
                    "task_index": 0,
                    "persona": "coder",
                    "duration_seconds": 0.1,
                    "outcome_metrics": {
                        "status": "completed",
                        "fix_attempts": 0,
                        "verify_attempts": 0,
                        "repo_key": "test_abc",
                        "issue_number": 0,
                        "duration_seconds": 0.1,
                        "changed_files_count": 0,
                    },
                    "error": None,
                    "stderr_snippet": None,
                },
            },
        ],
    )

    out = _run(tmp_path)
    runs_by_id = {r["run_id"]: r for r in out["runs"]}

    assert runs_by_id["r1"]["kind"] == "real"
    assert runs_by_id["s1"]["kind"] == "synthetic"

    # Default summary covers real only
    assert out["summary"]["total"] == 1
    assert out["summary"]["success_count"] == 1
    assert out["summary"]["success_rate"] == 1.0
    assert out["summary"]["status_histogram"]["completed"]["count"] == 1


def test_pipeline_inference(tmp_path: Path) -> None:
    # File with 'analyze' phase -> pr_review
    _write(
        tmp_path / "pr-review-run.jsonl",
        [
            {
                "run_id": "pr-review-run",
                "event": "llm.usage",
                "phase": "analyze",
                "data": {
                    "model": "composer-2.5",
                    "total_tokens": 20000,
                    "input_tokens": 15000,
                    "output_tokens": 2000,
                    "cache_read_tokens": 3000,
                    "cache_write_tokens": 0,
                    "duration_s": 20.0,
                    "agent_id": "a1",
                },
            },
        ],
    )
    # File with uppercase orchestration phases -> full
    _write(
        tmp_path / "full-pipeline-run.jsonl",
        [
            {
                "run_id": "full-pipeline-run",
                "event": "run.start",
                "data": {"title": "test run", "visual_audit": False, "phase_order": []},
                "persona": "backend",
                "issue_number": 99,
                "level": "info",
                "ts": "2026-05-29T00:00:00+00:00",
            },
            {
                "run_id": "full-pipeline-run",
                "event": "llm.usage",
                "phase": "PLAN",
                "data": {
                    "model": "composer-2.5",
                    "total_tokens": 30000,
                    "input_tokens": 20000,
                    "output_tokens": 5000,
                    "cache_read_tokens": 5000,
                    "cache_write_tokens": 0,
                    "duration_s": 40.0,
                    "agent_id": "a2",
                },
            },
            {
                "run_id": "full-pipeline-run",
                "event": "llm.usage",
                "phase": "IMPLEMENT",
                "data": {
                    "model": "composer-2.5",
                    "total_tokens": 40000,
                    "input_tokens": 25000,
                    "output_tokens": 6000,
                    "cache_read_tokens": 9000,
                    "cache_write_tokens": 0,
                    "duration_s": 50.0,
                    "agent_id": "a2",
                },
            },
        ],
    )

    out = _run(tmp_path)
    runs_by_id = {r["run_id"]: r for r in out["runs"]}

    assert runs_by_id["pr-review-run"]["pipeline"] == "pr_review"
    assert runs_by_id["full-pipeline-run"]["pipeline"] == "full"


def test_include_synthetic_flag(tmp_path: Path) -> None:
    _write(
        tmp_path / "synth2.jsonl",
        [
            {
                "run_id": "synth2",
                "event": "fleet.task.start",
                "data": {"persona": "coder", "goal": "g", "has_handoff": False, "task_index": 0},
            },
            {
                "run_id": "synth2",
                "event": "fleet.task.complete",
                "data": {
                    "status": "completed",
                    "task_index": 0,
                    "persona": "coder",
                    "duration_seconds": 0.1,
                    "outcome_metrics": {
                        "status": "completed",
                        "fix_attempts": 0,
                        "verify_attempts": 0,
                        "repo_key": "test_xyz",
                        "issue_number": 0,
                        "duration_seconds": 0.1,
                        "changed_files_count": 0,
                    },
                    "error": None,
                    "stderr_snippet": None,
                },
            },
        ],
    )

    out_default = _run(tmp_path)
    out_with_synth = _run(tmp_path, extra_args=["--include-synthetic"])

    # Default: synthetic excluded from summary
    assert out_default["summary"]["total"] == 0
    # With flag: synthetic included
    assert out_with_synth["summary"]["total"] == 1


def test_no_task_events_status_incomplete(tmp_path: Path) -> None:
    # A run.start file with no fleet.task events -> incomplete
    _write(
        tmp_path / "run-start-only.jsonl",
        [
            {
                "ts": "2026-05-29T00:00:00+00:00",
                "run_id": "run-start-only",
                "event": "run.start",
                "level": "info",
                "issue_number": 100,
                "persona": "frontend",
                "data": {"title": "some issue", "visual_audit": False},
            },
            {
                "ts": "2026-05-29T00:01:00+00:00",
                "run_id": "run-start-only",
                "event": "llm.usage",
                "level": "info",
                "phase": "PLAN",
                "data": {
                    "model": "composer-2.5",
                    "total_tokens": 50000,
                    "input_tokens": 30000,
                    "output_tokens": 5000,
                    "cache_read_tokens": 15000,
                    "cache_write_tokens": 0,
                    "duration_s": 40.0,
                    "agent_id": "a3",
                },
            },
        ],
    )

    out = _run(tmp_path)
    runs_by_id = {r["run_id"]: r for r in out["runs"]}
    r = runs_by_id["run-start-only"]

    assert r["kind"] == "real"
    assert r["status"] == "incomplete"
    assert r["success"] is False
    assert r["tokens_total"] == 50000


def test_fix_phase_tokens_tracked(tmp_path: Path) -> None:
    _write(
        tmp_path / "fix-tokens-run.jsonl",
        [
            {
                "run_id": "fix-tokens-run",
                "event": "llm.usage",
                "phase": "execute",
                "data": {
                    "model": "composer-2.5",
                    "total_tokens": 80000,
                    "input_tokens": 60000,
                    "output_tokens": 8000,
                    "cache_read_tokens": 12000,
                    "cache_write_tokens": 0,
                    "duration_s": 60.0,
                    "agent_id": "a4",
                },
            },
            {
                "run_id": "fix-tokens-run",
                "event": "llm.usage",
                "phase": "fix",
                "data": {
                    "model": "composer-2.5",
                    "total_tokens": 25000,
                    "input_tokens": 20000,
                    "output_tokens": 2000,
                    "cache_read_tokens": 3000,
                    "cache_write_tokens": 0,
                    "duration_s": 20.0,
                    "agent_id": "a4",
                },
            },
        ],
    )

    out = _run(tmp_path)
    runs_by_id = {r["run_id"]: r for r in out["runs"]}
    r = runs_by_id["fix-tokens-run"]

    assert r["tokens_total"] == 105000
    assert r["fix_phase_tokens"] == 25000
    assert r["pipeline"] == "simple"


def test_run_end_outcome_is_terminal_status(tmp_path: Path) -> None:
    """Orchestration/issue-loop runs end via run.end.outcome, not fleet.task.complete."""
    _write(
        tmp_path / "issue-loop-run.jsonl",
        [
            {
                "ts": "2026-05-24T20:24:16+00:00",
                "run_id": "issue-loop-run",
                "event": "run.start",
                "level": "info",
                "issue_number": 1860,
                "persona": "frontend",
                "data": {"title": "fix loading states", "visual_audit": True},
            },
            {
                "run_id": "issue-loop-run",
                "event": "llm.usage",
                "phase": "IMPLEMENT",
                "data": {
                    "model": "composer-2.5",
                    "total_tokens": 300000,
                    "input_tokens": 200000,
                    "output_tokens": 30000,
                    "cache_read_tokens": 70000,
                    "cache_write_tokens": 0,
                    "duration_s": 100.0,
                    "agent_id": "a5",
                },
            },
            {
                "ts": "2026-05-24T20:57:22+00:00",
                "run_id": "issue-loop-run",
                "event": "run.end",
                "level": "info",
                "issue_number": 1860,
                "persona": "frontend",
                "data": {
                    "outcome": "review_changes_requested",
                    "pr_number": 1883,
                    "jsonl": "/home/evan/.hermes/fleet/runs/issue-loop-run.jsonl",
                },
            },
        ],
    )

    out = _run(tmp_path)
    r = {x["run_id"]: x for x in out["runs"]}["issue-loop-run"]

    assert r["kind"] == "real"
    assert r["status"] == "review_changes_requested"
    assert r["success"] is False
    assert r["pr_number"] == 1883
    assert r["issue_number"] == 1860
    assert r["pipeline"] == "full"
    assert r["tokens_total"] == 300000


def test_run_end_completed_counts_as_success(tmp_path: Path) -> None:
    _write(
        tmp_path / "orch-completed.jsonl",
        [
            {
                "run_id": "orch-completed",
                "event": "run.start",
                "issue_number": 200,
                "persona": "backend",
                "data": {"title": "add endpoint", "visual_audit": False},
            },
            {
                "run_id": "orch-completed",
                "event": "llm.usage",
                "phase": "IMPLEMENT",
                "data": {
                    "model": "composer-2.5",
                    "total_tokens": 120000,
                    "input_tokens": 90000,
                    "output_tokens": 10000,
                    "cache_read_tokens": 20000,
                    "cache_write_tokens": 0,
                    "duration_s": 70.0,
                    "agent_id": "a6",
                },
            },
            {
                "run_id": "orch-completed",
                "event": "run.end",
                "issue_number": 200,
                "persona": "backend",
                "data": {"outcome": "completed", "pr_number": 201},
            },
        ],
    )

    out = _run(tmp_path)
    summary = out["summary"]
    r = {x["run_id"]: x for x in out["runs"]}["orch-completed"]

    assert r["status"] == "completed"
    assert r["success"] is True
    assert r["pr_number"] == 201
    assert summary["success_count"] == 1
    assert summary["status_histogram"]["completed"]["count"] == 1
