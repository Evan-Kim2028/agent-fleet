"""Tests for per-run outcome metrics used in logs and level-up experience."""

from __future__ import annotations

from agent_fleet.observability.run_metrics import (
    build_run_metrics,
    count_verify_fix_loops,
    extract_verify_failure,
)


def test_extract_verify_failure_from_dispatcher_verify_phase() -> None:
    phases = [
        {
            "phase": "verify",
            "command": "pytest -q",
            "exit_code": 1,
            "stderr": "FAILED tests/test_foo.py",
            "passed": False,
        }
    ]
    failure = extract_verify_failure(phases)
    assert failure is not None
    assert failure["kind"] == "verify"
    assert failure["command"] == "pytest -q"
    assert failure["exit_code"] == 1
    assert "FAILED" in failure["stderr_snippet"]


def test_extract_bootstrap_failure_from_runner_verify_dict() -> None:
    phases = {
        "VERIFY_0": {
            "severity": "fatal",
            "message": "Worktree bootstrap failed: bash scripts/fleet-worktree-bootstrap.sh",
            "checks": [
                {
                    "name": "bootstrap: bash scripts/fleet-worktree-bootstrap.sh",
                    "passed": False,
                    "exit_code": 127,
                    "stderr_tail": "command not found",
                }
            ],
        }
    }
    failure = extract_verify_failure(phases)
    assert failure is not None
    assert failure["kind"] == "bootstrap"
    assert "fleet-worktree-bootstrap" in failure["command"]


def test_count_verify_fix_loops_runner_style() -> None:
    phases = {"VERIFY_0": {}, "VERIFY_1": {}, "VERIFY_2": {}}
    verify_attempts, fix_attempts = count_verify_fix_loops(phases)
    assert verify_attempts == 3
    assert fix_attempts == 2


def test_build_run_metrics_cost_alerts() -> None:
    metrics = build_run_metrics(
        status="verify_failed",
        phases={"VERIFY_0": {}, "VERIFY_1": {}, "VERIFY_2": {}},
        usage_rollup={
            "totals": {"total_tokens": 1000},
            "by_phase": {
                "FIX": {"total_tokens": 600, "calls": 2},
                "VERIFY": {"total_tokens": 200, "calls": 3},
            },
        },
    )
    assert metrics["verify_attempts"] == 3
    assert "cost_alerts" in metrics
    assert "verify_retries_high" in metrics["cost_alerts"]
    assert "fix_phase_token_ratio_high" in metrics["cost_alerts"]
