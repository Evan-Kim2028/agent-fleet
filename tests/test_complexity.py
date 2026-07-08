"""Tests for complexity-driven runtime derivation (spec §v0.8.4)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_fleet.complexity import (
    RuntimeConfig,
    classify_complexity,
    coerce_complexity,
    derive_runtime,
    is_actionable_stderr,
    observe_token_ceiling,
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


def test_derive_runtime_invalid_loadout_size_raises() -> None:
    """A misspelled loadout_size in a tier override is rejected, not silently coerced."""
    with pytest.raises(ValueError, match="Invalid loadout_size"):
        derive_runtime("LOW", tier_overrides={"LOW": {"loadout_size": "ful"}})


def test_derive_runtime_valid_loadout_sizes_accepted() -> None:
    """All three valid loadout_size values are accepted without error."""
    for size in ("minimal", "standard", "full"):
        rt = derive_runtime("MED", tier_overrides={"MED": {"loadout_size": size}})
        assert rt.loadout_size == size


# ---------------------------------------------------------------------------
# Token ceiling metric (no mid-run abort)
# ---------------------------------------------------------------------------


def _make_run_log(run_id: str = "test-run") -> RunLog:
    ctx = RunContext(run_id=run_id)
    return RunLog(
        run_id=run_id,
        context=ctx,
        sinks=[],
    )


def test_observe_token_ceiling_returns_breach() -> None:
    run_log = _make_run_log()
    run_log.llm_usage(
        phase="execute",
        model="test",
        duration_s=0.1,
        input_tokens=600_000,
        output_tokens=500_000,
    )
    with bind_run(run_log, run_log.context):
        breach = observe_token_ceiling(token_ceiling=1_000_000, declared_complexity="LOW")
    assert breach is not None
    assert breach.observed_total_tokens == 1_100_000
    assert breach.over_by == 100_000
    assert breach.to_dict()["efficiency_ratio"] == 1.1


def test_token_ceiling_metric_does_not_abort_pipeline() -> None:
    """Over ceiling after execute: record metric, do not raise."""
    from agent_fleet.phases import run_pipeline

    run_log = _make_run_log()
    run_log.llm_usage(
        phase="execute",
        model="test",
        duration_s=0.1,
        input_tokens=500_001,
        output_tokens=500_001,
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

    with bind_run(run_log, run_log.context):
        phase_results, _summary, exit_code, _changed = run_pipeline(
            backend=backend,
            resolver=resolver,
            task=task,
            workspace=workspace,
            timeout_s=60,
            phases=["execute"],
            token_ceiling=1_000_000,
            declared_complexity="LOW",
        )

    assert exit_code == 0
    complexity_phases = [p for p in phase_results if p.get("phase") == "complexity"]
    assert len(complexity_phases) == 1
    assert complexity_phases[0]["metric_only"] is True
    assert complexity_phases[0]["observed_total_tokens"] > 1_000_000


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


# ---------------------------------------------------------------------------
# declared_complexity and observed_total_tokens always populated (Fix 3)
# ---------------------------------------------------------------------------


def test_build_task_result_populates_complexity_fields() -> None:
    """declared_complexity and observed_total_tokens are set on happy-path result."""
    from unittest.mock import MagicMock

    from agent_fleet.dispatcher_task import build_task_result
    from agent_fleet.hooks import FleetTask
    from agent_fleet.observability.context import bind_run
    from agent_fleet.observability.events import RunContext
    from agent_fleet.observability.log import RunLog

    run_log = RunLog(run_id="tr", context=RunContext(run_id="tr"), sinks=[])
    run_log.llm_usage(
        phase="execute",
        model="test",
        duration_s=0.5,
        input_tokens=1000,
        output_tokens=500,
    )

    task = FleetTask(goal="test task", complexity="MED")
    fleet_log = MagicMock()
    fleet_log.emit = MagicMock()

    phase_results: list[dict[str, object]] = [
        {
            "phase": "execute",
            "stdout": "done",
            "stderr": "",
            "exit_code": 0,
            "agent_id": "agent-1",
        }
    ]

    with bind_run(run_log, run_log.context):
        result = build_task_result(
            task_index=0,
            task=task,
            start=0.0,
            phase_results=phase_results,
            summary="done",
            exit_code=0,
            changed_files=["src/foo.py"],
            task_workspace=None,
            fleet_log=fleet_log,
        )

    assert result.declared_complexity == "MED"
    assert result.observed_total_tokens is not None
    assert result.observed_total_tokens > 0


def test_build_task_result_complexity_none_when_no_tokens() -> None:
    """With no LLM calls, observed_total_tokens is None but declared_complexity is set."""
    from unittest.mock import MagicMock

    from agent_fleet.dispatcher_task import build_task_result
    from agent_fleet.hooks import FleetTask

    task = FleetTask(goal="test task", complexity="LOW")
    fleet_log = MagicMock()
    fleet_log.emit = MagicMock()

    phase_results2: list[dict[str, object]] = [
        {
            "phase": "execute",
            "stdout": "done",
            "stderr": "",
            "exit_code": 0,
        }
    ]

    result = build_task_result(
        task_index=0,
        task=task,
        start=0.0,
        phase_results=phase_results2,
        summary="done",
        exit_code=0,
        changed_files=[],
        task_workspace=None,
        fleet_log=fleet_log,
    )

    assert result.declared_complexity == "LOW"
    # No LLM calls in this context → tokens are None (no run_log bound)
    # declared_complexity is always set regardless


def test_build_task_result_no_complexity_task() -> None:
    """declared_complexity is None when task.complexity is None."""
    from unittest.mock import MagicMock

    from agent_fleet.dispatcher_task import build_task_result
    from agent_fleet.hooks import FleetTask

    task = FleetTask(goal="no complexity task")
    fleet_log = MagicMock()
    fleet_log.emit = MagicMock()

    phase_results3: list[dict[str, object]] = [
        {"phase": "execute", "stdout": "done", "stderr": "", "exit_code": 0}
    ]

    result = build_task_result(
        task_index=0,
        task=task,
        start=0.0,
        phase_results=phase_results3,
        summary="done",
        exit_code=0,
        changed_files=[],
        task_workspace=None,
        fleet_log=fleet_log,
    )

    assert result.declared_complexity is None


# ---------------------------------------------------------------------------
# classify_complexity — deterministic tier derivation
# ---------------------------------------------------------------------------


def test_classify_complexity_short_simple_goal_is_low() -> None:
    assert classify_complexity("fix typo in README") == "LOW"


def test_classify_complexity_rename_is_low() -> None:
    assert classify_complexity("rename the variable foo to bar") == "LOW"


def test_classify_complexity_moderate_goal_is_low_by_default() -> None:
    # Short goal, no keywords, no files → LOW
    assert classify_complexity("add a unit test for the login endpoint") == "LOW"


def test_classify_complexity_scope_keyword_refactor_is_high() -> None:
    assert classify_complexity("refactor the authentication module") == "HIGH"


def test_classify_complexity_scope_keyword_migrate_is_high() -> None:
    assert classify_complexity("migrate the database schema to Postgres") == "HIGH"


def test_classify_complexity_scope_keyword_architecture_is_high() -> None:
    assert classify_complexity("redesign the service architecture for multi-tenancy") == "HIGH"


def test_classify_complexity_many_files_is_high() -> None:
    files = [f"src/module_{i}.py" for i in range(10)]
    assert classify_complexity("update all modules", changed_files=files) == "HIGH"


def test_classify_complexity_few_files_promotes_to_med() -> None:
    files = ["src/a.py", "src/b.py", "src/c.py"]
    assert classify_complexity("update imports in these files", changed_files=files) == "MED"


def test_classify_complexity_long_goal_is_med() -> None:
    # 65 words → MED (above _WORDS_HIGH=60, below _WORDS_VERY_HIGH=120)
    long_goal = " ".join(["word"] * 65)
    assert classify_complexity(long_goal) == "MED"


def test_classify_complexity_very_long_goal_is_high() -> None:
    # 125 words → HIGH
    very_long_goal = " ".join(["word"] * 125)
    assert classify_complexity(very_long_goal) == "HIGH"


def test_classify_complexity_no_files_arg_same_as_empty() -> None:
    assert classify_complexity("add logging") == classify_complexity(
        "add logging", changed_files=[]
    )


def test_classify_complexity_explicit_complexity_overrides_classifier() -> None:
    """An explicit caller-supplied complexity on FleetTask always wins."""
    from agent_fleet.dispatcher import _normalize_tasks

    # A "refactor" goal would be classified HIGH, but caller says LOW.
    tasks, _ = _normalize_tasks(
        goal=None,
        context=None,
        persona=None,
        workspace=None,
        pipeline=None,
        tasks=[{"goal": "refactor the entire authentication system", "complexity": "LOW"}],
        complexity=None,
    )
    assert tasks[0].complexity == "LOW"


def test_classify_complexity_auto_fills_when_no_explicit_complexity() -> None:
    """When no explicit complexity, _normalize_tasks fills it via classifier."""
    from agent_fleet.dispatcher import _normalize_tasks

    tasks, _ = _normalize_tasks(
        goal="refactor the entire authentication system",
        context=None,
        persona=None,
        workspace=None,
        pipeline=None,
        tasks=None,
        complexity=None,
    )
    # "refactor" triggers HIGH
    assert tasks[0].complexity == "HIGH"


def test_classify_complexity_explicit_single_goal_overrides() -> None:
    """An explicit complexity on single-goal dispatch wins over classifier."""
    from agent_fleet.dispatcher import _normalize_tasks

    tasks, _ = _normalize_tasks(
        goal="refactor the entire system",
        context=None,
        persona=None,
        workspace=None,
        pipeline=None,
        tasks=None,
        complexity="LOW",
    )
    assert tasks[0].complexity == "LOW"


def test_normalize_tasks_single_goal_defaults_to_extend_empty_skills() -> None:
    """Existing behavior preserved: no skills/skills_mode args -> extend, empty skills."""
    from agent_fleet.dispatcher import _normalize_tasks

    tasks, _ = _normalize_tasks(
        goal="fix bug",
        context=None,
        persona=None,
        workspace=None,
        pipeline=None,
        tasks=None,
    )
    assert tasks[0].skills == ()
    assert tasks[0].skills_mode == "extend"


def test_normalize_tasks_single_goal_forwards_skills_and_mode() -> None:
    from agent_fleet.dispatcher import _normalize_tasks

    tasks, _ = _normalize_tasks(
        goal="fix bug",
        context=None,
        persona=None,
        workspace=None,
        pipeline=None,
        tasks=None,
        skills=("cursor-team-kit/verify-this",),
        skills_mode="replace",
    )
    assert tasks[0].skills == ("cursor-team-kit/verify-this",)
    assert tasks[0].skills_mode == "replace"


def test_normalize_tasks_batch_entry_uses_fleet_level_skills_default() -> None:
    """Batch entries without their own 'skills' key inherit the fleet-level default."""
    from agent_fleet.dispatcher import _normalize_tasks

    tasks, _ = _normalize_tasks(
        goal=None,
        context=None,
        persona=None,
        workspace=None,
        pipeline=None,
        tasks=[{"goal": "task A"}],
        skills=("a/skill",),
        skills_mode="replace",
    )
    assert tasks[0].skills == ("a/skill",)
    assert tasks[0].skills_mode == "replace"


def test_normalize_tasks_batch_entry_overrides_fleet_level_skills() -> None:
    """A per-entry 'skills' key overrides the fleet-level default."""
    from agent_fleet.dispatcher import _normalize_tasks

    tasks, _ = _normalize_tasks(
        goal=None,
        context=None,
        persona=None,
        workspace=None,
        pipeline=None,
        tasks=[{"goal": "task A", "skills": ["b/skill"], "skills_mode": "extend"}],
        skills=("a/skill",),
        skills_mode="replace",
    )
    assert tasks[0].skills == ("b/skill",)
    assert tasks[0].skills_mode == "extend"
