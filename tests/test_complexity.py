"""Tests for complexity-driven runtime derivation (spec §v0.8.4)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_fleet.complexity import (
    RuntimeConfig,
    TokenCeilingExceeded,
    coerce_complexity,
    derive_runtime,
    is_actionable_stderr,
)
from agent_fleet.hooks import FleetTask
from agent_fleet.observability.context import bind_run
from agent_fleet.observability.events import RunContext
from agent_fleet.observability.log import RunLog

# ---------------------------------------------------------------------------
# derive_runtime mapping
# ---------------------------------------------------------------------------


def test_derive_runtime_low() -> None:
    rt = derive_runtime("LOW")
    assert rt == RuntimeConfig(
        pipeline="simple",
        retries=1,
        token_ceiling=1_000_000,
        loadout_size="minimal",
    )


def test_derive_runtime_med() -> None:
    rt = derive_runtime("MED")
    assert rt == RuntimeConfig(
        pipeline="code_review",
        retries=1,
        token_ceiling=5_000_000,
        loadout_size="standard",
    )


def test_derive_runtime_high() -> None:
    rt = derive_runtime("HIGH")
    assert rt == RuntimeConfig(
        pipeline="code_review",
        retries=2,
        token_ceiling=20_000_000,
        loadout_size="full",
    )


# ---------------------------------------------------------------------------
# Default complexity is MED when unspecified
# ---------------------------------------------------------------------------


def test_default_complexity_is_med_when_none() -> None:
    rt = derive_runtime(None)
    assert rt == derive_runtime("MED")


def test_coerce_complexity_none_returns_med() -> None:
    assert coerce_complexity(None) == "MED"


def test_coerce_complexity_invalid_raises() -> None:
    with pytest.raises(ValueError, match="Invalid complexity"):
        coerce_complexity("EXTREME")


def test_coerce_complexity_case_insensitive() -> None:
    assert coerce_complexity("low") == "LOW"
    assert coerce_complexity("Med") == "MED"
    assert coerce_complexity("HIGH") == "HIGH"


def test_fleet_task_complexity_defaults_none() -> None:
    task = FleetTask(goal="do something")
    assert task.complexity is None


# ---------------------------------------------------------------------------
# Token ceiling abort
# ---------------------------------------------------------------------------


def _make_run_log(run_id: str = "test-run") -> RunLog:
    ctx = RunContext(run_id=run_id)
    return RunLog(
        run_id=run_id,
        context=ctx,
        sinks=[],
    )


def test_token_ceiling_abort_fires() -> None:
    """When cumulative tokens exceed the ceiling, run_pipeline raises TokenCeilingExceeded."""
    from agent_fleet.phases import run_pipeline

    # Build a RunLog with already-accumulated tokens above the ceiling.
    run_log = _make_run_log()
    # Inject usage that exceeds a LOW ceiling (1_000_000).
    run_log.llm_usage(
        phase="execute",
        model="test",
        duration_s=0.1,
        input_tokens=500_001,
        output_tokens=500_001,  # total = 1_000_002 > 1_000_000
    )

    backend = MagicMock()
    backend.run.return_value = MagicMock(
        stdout="done", stderr="", exit_code=0, duration_s=0.1, agent_id=None
    )

    resolver = MagicMock()
    resolver.load.return_value = MagicMock(
        name="coder",
        allowed_tools=[],
        body="be a coder",
        extra_instructions="",
        allowed_paths=(),
        model="model",
        mode="agent",
    )

    task = FleetTask(goal="test task", complexity="LOW")
    workspace = Path("/tmp")

    with bind_run(run_log, run_log.context), pytest.raises(TokenCeilingExceeded) as exc_info:
        run_pipeline(
            backend=backend,
            resolver=resolver,
            task=task,
            workspace=workspace,
            timeout_s=60,
            phases=["execute"],
            token_ceiling=1_000_000,
            declared_complexity="LOW",
        )

    assert exc_info.value.declared_complexity == "LOW"
    assert exc_info.value.observed_total_tokens > 1_000_000
    assert exc_info.value.ceiling == 1_000_000


def test_token_ceiling_no_abort_when_under() -> None:
    """When tokens are under the ceiling, no exception is raised."""
    from agent_fleet.phases import run_pipeline

    run_log = _make_run_log()
    run_log.llm_usage(
        phase="execute",
        model="test",
        duration_s=0.1,
        input_tokens=100,
        output_tokens=100,  # total = 200, well under 1_000_000
    )

    backend = MagicMock()
    backend.run.return_value = MagicMock(
        stdout="done", stderr="", exit_code=0, duration_s=0.1, agent_id=None
    )

    resolver = MagicMock()
    resolver.load.return_value = MagicMock(
        name="coder",
        allowed_tools=[],
        body="be a coder",
        extra_instructions="",
        allowed_paths=(),
        model="model",
        mode="agent",
    )

    task = FleetTask(goal="test task", complexity="MED")
    workspace = Path("/tmp")

    with bind_run(run_log, run_log.context):
        # Should not raise
        run_pipeline(
            backend=backend,
            resolver=resolver,
            task=task,
            workspace=workspace,
            timeout_s=60,
            phases=["execute"],
            token_ceiling=1_000_000,
            declared_complexity="MED",
        )


# ---------------------------------------------------------------------------
# LOW retry gate
# ---------------------------------------------------------------------------


def test_is_actionable_stderr_empty_stderr_is_not_actionable() -> None:
    assert not is_actionable_stderr("", ("src/foo.py",))
    assert not is_actionable_stderr("   ", ("src/foo.py",))


def test_is_actionable_stderr_no_written_files_is_not_actionable() -> None:
    assert not is_actionable_stderr("error in src/foo.py line 10", ())
    assert not is_actionable_stderr("error in src/foo.py line 10", [])


def test_is_actionable_stderr_generic_stderr_no_file_mention_is_not_actionable() -> None:
    assert not is_actionable_stderr(
        "DeprecationWarning: some generic warning",
        ("src/foo.py",),
    )


def test_is_actionable_stderr_basename_match_is_actionable() -> None:
    assert is_actionable_stderr(
        "SyntaxError: foo.py line 5: unexpected indent",
        ("src/foo.py",),
    )


def test_is_actionable_stderr_full_path_match_is_actionable() -> None:
    assert is_actionable_stderr(
        "error in src/foo.py",
        ("src/foo.py",),
    )


def test_is_actionable_stderr_multiple_written_files_match_any() -> None:
    assert is_actionable_stderr(
        "test_bar.py: assertion failed",
        ("src/foo.py", "tests/test_bar.py"),
    )
    assert not is_actionable_stderr(
        "something unrelated",
        ("src/foo.py", "tests/test_bar.py"),
    )
