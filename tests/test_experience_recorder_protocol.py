"""Regression: ExperienceRecorder Protocol injection into runner and dispatcher.

Verifies that:
- A custom recorder injected into LocalFleetRunner receives record_runner_experience calls.
- A custom recorder injected into FleetDispatcher receives record_completed_task_experience calls.
- A no-op recorder silences recording without error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from agent_fleet.config import load_fleet_config
from agent_fleet.hooks import ExperienceRecorder, FleetTask, LevelUpRecorder
from agent_fleet.runner import LocalFleetRunner

if TYPE_CHECKING:
    import pytest

    from agent_fleet.observability.fleet_logger import FleetLogger
    from agent_fleet.runner import FleetRunResult

ROOT = Path(__file__).resolve().parent.parent


# --- Spy recorders for assertions ---


@dataclass
class _RunnerCall:
    result: FleetRunResult
    title: str
    persona: str
    repo_root: Path
    experience_source: str
    pr_loop_round: int | None
    dispatch_equip: object


@dataclass
class _TaskCall:
    task_index: int
    task: FleetTask
    status: str
    phase_results: list[dict[str, object]]
    changed_files: list[str] | None
    workspace: Path | None
    fleet_log: FleetLogger
    duration_seconds: float | None
    error: str | None


@dataclass
class SpyRecorder:
    """Records all calls for assertions."""

    runner_calls: list[_RunnerCall] = field(default_factory=list)
    task_calls: list[_TaskCall] = field(default_factory=list)

    def record_runner_experience(
        self,
        *,
        result: FleetRunResult,
        title: str,
        persona: str,
        repo_root: Path,
        experience_source: str = "full_pipeline",
        pr_loop_round: int | None = None,
        dispatch_equip: object = None,
    ) -> None:
        self.runner_calls.append(
            _RunnerCall(
                result=result,
                title=title,
                persona=persona,
                repo_root=repo_root,
                experience_source=experience_source,
                pr_loop_round=pr_loop_round,
                dispatch_equip=dispatch_equip,
            )
        )

    def record_completed_task_experience(
        self,
        *,
        task_index: int,
        task: FleetTask,
        status: str,
        phase_results: list[dict[str, object]],
        changed_files: list[str] | None,
        workspace: Path | None,
        fleet_log: FleetLogger,
        duration_seconds: float | None = None,
        error: str | None = None,
    ) -> None:
        self.task_calls.append(
            _TaskCall(
                task_index=task_index,
                task=task,
                status=status,
                phase_results=phase_results,
                changed_files=changed_files,
                workspace=workspace,
                fleet_log=fleet_log,
                duration_seconds=duration_seconds,
                error=error,
            )
        )


class NoOpRecorder:
    """Silently discards all experience recording."""

    def record_runner_experience(self, **_kwargs: object) -> None:
        pass

    def record_completed_task_experience(self, **_kwargs: object) -> None:
        pass


def _make_runner(recorder: ExperienceRecorder | None = None) -> LocalFleetRunner:
    fc = load_fleet_config(ROOT / "fleet.example.yaml")
    return LocalFleetRunner(
        backend=MagicMock(),
        persona_resolver=MagicMock(),
        git_ops=MagicMock(),
        verifier=MagicMock(),
        fleet_config=fc,
        experience_recorder=recorder,
    )


# --- Protocol structural checks ---


def test_spy_recorder_satisfies_protocol() -> None:
    assert isinstance(SpyRecorder(), ExperienceRecorder)


def test_noop_recorder_satisfies_protocol() -> None:
    assert isinstance(NoOpRecorder(), ExperienceRecorder)


def test_level_up_recorder_satisfies_protocol() -> None:
    assert isinstance(LevelUpRecorder(), ExperienceRecorder)


# --- Runner injection tests ---


def test_runner_stores_injected_recorder() -> None:
    """Custom recorder injected into LocalFleetRunner is stored (not replaced)."""
    spy = SpyRecorder()
    runner = _make_runner(spy)
    assert runner._experience_recorder is spy


def test_runner_defaults_to_level_up_recorder() -> None:
    """Omitting experience_recorder gives a LevelUpRecorder by default."""
    runner = _make_runner()
    assert isinstance(runner._experience_recorder, LevelUpRecorder)


def test_noop_recorder_in_runner_does_not_raise() -> None:
    """NoOpRecorder silences recording without error when called through the seam."""
    from agent_fleet.runner import FleetRunResult

    noop = NoOpRecorder()
    runner = _make_runner(noop)
    assert runner._experience_recorder is noop

    fake = FleetRunResult(run_id="x", task_id=1, persona="coder", outcome="completed")
    # Must not raise.
    runner._experience_recorder.record_runner_experience(
        result=fake,
        title="t",
        persona="coder",
        repo_root=ROOT,
    )


def test_spy_recorder_in_runner_receives_call() -> None:
    """SpyRecorder receives record_runner_experience when called through the seam."""
    from agent_fleet.runner import FleetRunResult

    spy = SpyRecorder()
    runner = _make_runner(spy)

    fake = FleetRunResult(run_id="r1", task_id=42, persona="coder", outcome="completed")
    runner._experience_recorder.record_runner_experience(
        result=fake,
        title="Fix foo",
        persona="coder",
        repo_root=ROOT,
        experience_source="issue_dispatch",
    )

    assert len(spy.runner_calls) == 1
    call = spy.runner_calls[0]
    assert call.title == "Fix foo"
    assert call.experience_source == "issue_dispatch"
    assert call.result.outcome == "completed"


# --- Dispatcher injection tests ---


def test_dispatcher_stores_injected_recorder() -> None:
    """Custom recorder injected into FleetDispatcher is stored."""
    from agent_fleet.dispatcher import FleetDispatcher

    spy = SpyRecorder()
    fc = load_fleet_config(ROOT / "fleet.example.yaml")
    dispatcher = FleetDispatcher(config=fc, experience_recorder=spy)

    assert dispatcher._experience_recorder is spy


def test_dispatcher_defaults_to_level_up_recorder() -> None:
    """Omitting experience_recorder gives a LevelUpRecorder."""
    from agent_fleet.dispatcher import FleetDispatcher

    fc = load_fleet_config(ROOT / "fleet.example.yaml")
    dispatcher = FleetDispatcher(config=fc)

    assert isinstance(dispatcher._experience_recorder, LevelUpRecorder)


def test_dispatcher_spy_receives_task_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """SpyRecorder receives record_completed_task_experience when a task runs."""
    from agent_fleet.dispatcher import FleetDispatcher
    from agent_fleet.hooks import FleetTaskResult

    spy = SpyRecorder()
    fc = load_fleet_config(ROOT / "fleet.example.yaml")
    dispatcher = FleetDispatcher(config=fc, experience_recorder=spy)

    fake_task_result = FleetTaskResult(
        task_index=0,
        persona="coder",
        goal="x",
        status="completed",
        summary=None,
        error=None,
        duration_seconds=0.1,
    )

    def fake_execute_task(  # noqa: ANN202
        self,  # noqa: ANN001
        task_index: int,
        task: FleetTask,
        **_kwargs: object,
    ):
        # Simulate the real seam: call the recorder, then return a result.
        from agent_fleet.observability.fleet_logger import FleetLogger

        fl = FleetLogger.for_dispatch(task_index=task_index, persona=task.persona)
        self._experience_recorder.record_completed_task_experience(
            task_index=task_index,
            task=task,
            status="completed",
            phase_results=[],
            changed_files=None,
            workspace=None,
            fleet_log=fl,
        )
        return fake_task_result

    monkeypatch.setattr(FleetDispatcher, "_execute_task", fake_execute_task)

    results = dispatcher.dispatch(goal="x", persona="coder", workspace=str(ROOT), pipeline="simple")
    assert results[0].status == "completed"
    assert len(spy.task_calls) == 1
    assert spy.task_calls[0].status == "completed"
    assert spy.task_calls[0].task_index == 0
