"""Regression: token-ceiling enforcement in run_configured_pipeline.

Verifies that:
- With enforce_token_ceiling=True, a breach returns exit_code=1 and a
  ceiling_abort phase (not log-only).
- With enforce_token_ceiling=False (default), a breach is log-only: exit_code
  is unchanged and no ceiling_abort phase is added.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from agent_fleet.observability.context import bind_run
from agent_fleet.observability.events import RunContext
from agent_fleet.observability.log import RunLog


def _make_run_log(run_id: str = "test-run") -> RunLog:
    ctx = RunContext(run_id=run_id)
    return RunLog(run_id=run_id, context=ctx, sinks=[])


def _fake_backend() -> MagicMock:
    backend = MagicMock()
    backend.run.return_value = MagicMock(
        stdout="done", stderr="", exit_code=0, duration_s=0.1, agent_id=None
    )
    return backend


def _fake_resolver() -> MagicMock:
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
    return resolver


ROOT = Path(__file__).resolve().parent.parent


def test_enforce_ceiling_aborts_when_breached() -> None:
    """With enforce_token_ceiling=True and a breach, exit_code=1 and a ceiling_abort phase."""
    from agent_fleet.config import load_fleet_config
    from agent_fleet.dispatcher_task import run_configured_pipeline
    from agent_fleet.hooks import FleetTask

    run_log = _make_run_log()
    # Record usage that exceeds a 1_000_000 ceiling
    run_log.llm_usage(
        phase="execute",
        model="test",
        duration_s=0.1,
        input_tokens=600_000,
        output_tokens=500_000,
    )

    task = FleetTask(goal="test task", complexity="LOW")
    task_config = load_fleet_config(ROOT / "fleet.example.yaml")
    task_config.enforce_token_ceiling = True  # opt-in enforcement

    with bind_run(run_log, run_log.context):
        phase_results, summary, exit_code, _changed = run_configured_pipeline(
            backend=_fake_backend(),
            resolver=_fake_resolver(),
            task=task,
            run_workspace=Path("/tmp"),
            task_config=task_config,
            phases=["execute"],
            repo_config=None,
            git_repo=None,
            handoff=None,
            token_ceiling=1_000_000,
            declared_complexity="LOW",
            enforce_token_ceiling=True,
        )

    assert exit_code == 1
    abort_phases = [p for p in phase_results if p.get("phase") == "ceiling_abort"]
    assert len(abort_phases) == 1
    assert abort_phases[0]["enforced"] is True
    observed = abort_phases[0]["observed_total_tokens"]
    assert isinstance(observed, int) and observed > 1_000_000
    assert "ceiling enforced" in summary.lower()


def test_default_no_enforcement_log_only_when_breached() -> None:
    """With enforce_token_ceiling=False (default), a breach is log-only: no abort."""
    from agent_fleet.config import load_fleet_config
    from agent_fleet.dispatcher_task import run_configured_pipeline
    from agent_fleet.hooks import FleetTask

    run_log = _make_run_log()
    run_log.llm_usage(
        phase="execute",
        model="test",
        duration_s=0.1,
        input_tokens=600_000,
        output_tokens=500_000,
    )

    task = FleetTask(goal="test task", complexity="LOW")
    task_config = load_fleet_config(ROOT / "fleet.example.yaml")
    # enforce_token_ceiling defaults to False — current log-only behavior

    with bind_run(run_log, run_log.context):
        phase_results, _summary, exit_code, _changed = run_configured_pipeline(
            backend=_fake_backend(),
            resolver=_fake_resolver(),
            task=task,
            run_workspace=Path("/tmp"),
            task_config=task_config,
            phases=["execute"],
            repo_config=None,
            git_repo=None,
            handoff=None,
            token_ceiling=1_000_000,
            declared_complexity="LOW",
            # enforce_token_ceiling defaults to False
        )

    # Behavior unchanged: no abort, exit_code from the actual pipeline (0)
    assert exit_code == 0
    abort_phases = [p for p in phase_results if p.get("phase") == "ceiling_abort"]
    assert len(abort_phases) == 0
    # The log-only metric phase is still present
    metric_phases = [p for p in phase_results if p.get("phase") == "complexity"]
    assert len(metric_phases) == 1
    assert metric_phases[0]["metric_only"] is True


def test_enforce_ceiling_no_breach_passes_through() -> None:
    """With enforce_token_ceiling=True but no breach, outcome is unchanged."""
    from agent_fleet.config import load_fleet_config
    from agent_fleet.dispatcher_task import run_configured_pipeline
    from agent_fleet.hooks import FleetTask

    run_log = _make_run_log()
    run_log.llm_usage(
        phase="execute",
        model="test",
        duration_s=0.1,
        input_tokens=100,
        output_tokens=100,  # well under 1_000_000
    )

    task = FleetTask(goal="test task", complexity="LOW")
    task_config = load_fleet_config(ROOT / "fleet.example.yaml")
    task_config.enforce_token_ceiling = True

    with bind_run(run_log, run_log.context):
        phase_results, _summary, exit_code, _changed = run_configured_pipeline(
            backend=_fake_backend(),
            resolver=_fake_resolver(),
            task=task,
            run_workspace=Path("/tmp"),
            task_config=task_config,
            phases=["execute"],
            repo_config=None,
            git_repo=None,
            handoff=None,
            token_ceiling=1_000_000,
            declared_complexity="LOW",
            enforce_token_ceiling=True,
        )

    assert exit_code == 0
    abort_phases = [p for p in phase_results if p.get("phase") == "ceiling_abort"]
    assert len(abort_phases) == 0
