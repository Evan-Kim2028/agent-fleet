"""Tests for review-run gating on small green diffs (U4 re-review + U6 first pass)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

if TYPE_CHECKING:
    import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _make_task() -> MagicMock:
    task = MagicMock()
    task.persona = "coder"
    task.allowed_paths = ()
    return task


def _make_persona() -> MagicMock:
    persona = MagicMock()
    persona.name = "coder"
    persona.allowed_paths = ()
    return persona


def _make_resolver(persona: MagicMock) -> MagicMock:
    resolver = MagicMock()
    resolver.load.return_value = persona
    return resolver


def _green_scope_result() -> dict[str, object]:
    return {"phase": "scope", "passed": True, "exit_code": 0, "stdout": "", "stderr": ""}


def _green_verify_result() -> dict[str, object]:
    return {"phase": "verify", "passed": True, "exit_code": 0, "stdout": "", "stderr": ""}


def _failing_verify_result() -> dict[str, object]:
    return {
        "phase": "verify",
        "passed": False,
        "exit_code": 1,
        "stdout": "",
        "stderr": "tests failed",
    }


def test_review_skipped_when_green_gates_and_small_diff(tmp_path: Path) -> None:
    """When gates are green and changed lines < threshold, review is skipped."""
    from agent_fleet.code_review.loop import _rerun_quality_gates
    from agent_fleet.phases import REVIEW_SKIP_LINES_THRESHOLD

    persona = _make_persona()
    resolver = _make_resolver(persona)
    task = _make_task()

    _loop = "agent_fleet.code_review.loop"
    with (
        patch(f"{_loop}.collect_changed_files", return_value=["src/a.py"]),
        patch(f"{_loop}.run_scope_phase", return_value=_green_scope_result()),
        patch(f"{_loop}.run_verify_phases", return_value=[_green_verify_result()]),
        patch(f"{_loop}.changed_lines", return_value=REVIEW_SKIP_LINES_THRESHOLD - 1),
        patch(f"{_loop}.run_structured_review_phase") as mock_review,
    ):
        results, _summary, exit_code, _files = _rerun_quality_gates(
            backend=MagicMock(),
            resolver=resolver,
            task=task,
            workspace=tmp_path,
            timeout_s=60,
            repo=None,
            implementation_summary="done",
        )

    mock_review.assert_not_called()
    assert exit_code == 0
    review_results = [r for r in results if r.get("phase") == "review"]
    assert len(review_results) == 1
    r = review_results[0]
    assert r["skipped"] is True
    assert r["verdict"] == "approve"
    assert r["passed"] is True
    assert r["exit_code"] == 0
    assert str(REVIEW_SKIP_LINES_THRESHOLD) in r["reason"]


def test_review_runs_when_diff_at_threshold(tmp_path: Path) -> None:
    """When changed lines == threshold, review is NOT skipped."""
    from agent_fleet.code_review.loop import _rerun_quality_gates
    from agent_fleet.phases import REVIEW_SKIP_LINES_THRESHOLD

    persona = _make_persona()
    resolver = _make_resolver(persona)
    task = _make_task()

    review_result = {
        "phase": "review",
        "verdict": "approve",
        "skipped": False,
        "passed": True,
        "exit_code": 0,
        "summary": "looks good",
        "stdout": "",
        "stderr": "",
    }

    _loop = "agent_fleet.code_review.loop"
    with (
        patch(f"{_loop}.collect_changed_files", return_value=["src/a.py"]),
        patch(f"{_loop}.run_scope_phase", return_value=_green_scope_result()),
        patch(f"{_loop}.run_verify_phases", return_value=[_green_verify_result()]),
        patch(f"{_loop}.changed_lines", return_value=REVIEW_SKIP_LINES_THRESHOLD),
        patch(f"{_loop}.run_structured_review_phase", return_value=review_result) as mock_review,
    ):
        results, _summary, exit_code, _files = _rerun_quality_gates(
            backend=MagicMock(),
            resolver=resolver,
            task=task,
            workspace=tmp_path,
            timeout_s=60,
            repo=None,
            implementation_summary="done",
        )

    mock_review.assert_called_once()
    assert exit_code == 0
    review_results = [r for r in results if r.get("phase") == "review"]
    assert len(review_results) == 1
    assert review_results[0].get("skipped") is False


def test_review_runs_when_diff_above_threshold(tmp_path: Path) -> None:
    """When changed lines > threshold, review runs normally."""
    from agent_fleet.code_review.loop import _rerun_quality_gates
    from agent_fleet.phases import REVIEW_SKIP_LINES_THRESHOLD

    persona = _make_persona()
    resolver = _make_resolver(persona)
    task = _make_task()

    review_result = {
        "phase": "review",
        "verdict": "approve",
        "skipped": False,
        "passed": True,
        "exit_code": 0,
        "summary": "looks good",
        "stdout": "",
        "stderr": "",
    }

    _loop = "agent_fleet.code_review.loop"
    with (
        patch(f"{_loop}.collect_changed_files", return_value=["src/a.py"]),
        patch(f"{_loop}.run_scope_phase", return_value=_green_scope_result()),
        patch(f"{_loop}.run_verify_phases", return_value=[_green_verify_result()]),
        patch(f"{_loop}.changed_lines", return_value=REVIEW_SKIP_LINES_THRESHOLD + 100),
        patch(f"{_loop}.run_structured_review_phase", return_value=review_result) as mock_review,
    ):
        _results, _summary, exit_code, _files = _rerun_quality_gates(
            backend=MagicMock(),
            resolver=resolver,
            task=task,
            workspace=tmp_path,
            timeout_s=60,
            repo=None,
            implementation_summary="done",
        )

    mock_review.assert_called_once()
    assert exit_code == 0


def test_review_runs_when_verify_failed(tmp_path: Path) -> None:
    """When verify fails, gate never fires — review path is not reached."""
    from agent_fleet.code_review.loop import _rerun_quality_gates

    persona = _make_persona()
    resolver = _make_resolver(persona)
    task = _make_task()

    _loop = "agent_fleet.code_review.loop"
    with (
        patch(f"{_loop}.collect_changed_files", return_value=["src/a.py"]),
        patch(f"{_loop}.run_scope_phase", return_value=_green_scope_result()),
        patch(f"{_loop}.run_verify_phases", return_value=[_failing_verify_result()]),
        patch(f"{_loop}.changed_lines", return_value=5) as mock_cl,
        patch(f"{_loop}.run_structured_review_phase") as mock_review,
    ):
        results, _summary, exit_code, _files = _rerun_quality_gates(
            backend=MagicMock(),
            resolver=resolver,
            task=task,
            workspace=tmp_path,
            timeout_s=60,
            repo=None,
            implementation_summary="done",
        )

    mock_cl.assert_not_called()
    mock_review.assert_not_called()
    assert exit_code == 1
    review_results = [r for r in results if r.get("phase") == "review"]
    assert len(review_results) == 0


def test_skip_threshold_default_value() -> None:
    """The threshold constant must be 50."""
    from agent_fleet.phases import REVIEW_SKIP_LINES_THRESHOLD

    assert REVIEW_SKIP_LINES_THRESHOLD == 50


def _patch_green_execute(
    monkeypatch: pytest.MonkeyPatch, *, verify_results: list[dict[str, object]], n_changed: int
) -> None:
    monkeypatch.setattr(
        "agent_fleet.phases.run_execute_phase",
        lambda **_k: {"phase": "execute", "stdout": "done", "exit_code": 0, "stderr": ""},
    )
    monkeypatch.setattr(
        "agent_fleet.phases.run_scope_phase",
        lambda **_k: {"phase": "scope", "passed": True, "exit_code": 0},
    )
    monkeypatch.setattr("agent_fleet.phases.run_verify_phases", lambda **_k: verify_results)
    monkeypatch.setattr("agent_fleet.phases.collect_changed_files", lambda _ws: ["a.py"])
    monkeypatch.setattr("agent_fleet.phases.changed_lines", lambda _ws: n_changed)


def _run_execute_review_pipeline(tmp_path: Path) -> tuple:
    from agent_fleet.config import load_fleet_config
    from agent_fleet.hooks import FleetTask
    from agent_fleet.personas import YamlPersonaResolver
    from agent_fleet.phases import run_pipeline

    fleet_config = load_fleet_config(_ROOT / "fleet.example.yaml")
    resolver = YamlPersonaResolver(fleet_config)
    task = FleetTask(goal="tiny fix", persona="coder", workspace=str(tmp_path))
    return run_pipeline(
        backend=MagicMock(),
        resolver=resolver,
        task=task,
        workspace=tmp_path,
        timeout_s=30,
        phases=["execute", "review"],
        repo=None,
    )


def test_first_pass_review_skipped_on_green_small_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_pipeline skips the first-pass structured review on green + small diff."""
    from agent_fleet.phases import REVIEW_SKIP_LINES_THRESHOLD

    _patch_green_execute(
        monkeypatch,
        verify_results=[{"phase": "verify", "passed": True, "exit_code": 0}],
        n_changed=REVIEW_SKIP_LINES_THRESHOLD - 1,
    )
    monkeypatch.setattr(
        "agent_fleet.phases.run_structured_review_phase",
        lambda **_k: (_ for _ in ()).throw(AssertionError("review must be skipped")),
    )

    results, _summary, exit_code, _files = _run_execute_review_pipeline(tmp_path)

    assert exit_code == 0
    review = [r for r in results if r.get("phase") == "review"]
    assert len(review) == 1
    assert review[0]["skipped"] is True
    assert review[0]["verdict"] == "approve"


def test_first_pass_review_runs_on_large_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_pipeline runs the first-pass review when the diff exceeds the threshold."""
    from agent_fleet.phases import REVIEW_SKIP_LINES_THRESHOLD

    review_result = {
        "phase": "review",
        "verdict": "approve",
        "skipped": False,
        "passed": True,
        "exit_code": 0,
        "summary": "ok",
        "stdout": "",
        "stderr": "",
    }
    _patch_green_execute(
        monkeypatch,
        verify_results=[{"phase": "verify", "passed": True, "exit_code": 0}],
        n_changed=REVIEW_SKIP_LINES_THRESHOLD + 100,
    )
    review_mock = MagicMock(return_value=review_result)
    monkeypatch.setattr("agent_fleet.phases.run_structured_review_phase", review_mock)

    results, _summary, exit_code, _files = _run_execute_review_pipeline(tmp_path)

    review_mock.assert_called_once()
    assert exit_code == 0
    review = [r for r in results if r.get("phase") == "review"]
    assert review and review[0].get("skipped") is not True


def test_first_pass_review_runs_when_no_verify_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no verify gate there is nothing green to lean on, so review runs."""
    from agent_fleet.phases import REVIEW_SKIP_LINES_THRESHOLD

    review_result = {
        "phase": "review",
        "verdict": "approve",
        "skipped": False,
        "passed": True,
        "exit_code": 0,
        "summary": "ok",
        "stdout": "",
        "stderr": "",
    }
    _patch_green_execute(
        monkeypatch,
        verify_results=[],
        n_changed=REVIEW_SKIP_LINES_THRESHOLD - 1,
    )
    review_mock = MagicMock(return_value=review_result)
    monkeypatch.setattr("agent_fleet.phases.run_structured_review_phase", review_mock)

    _results, _summary, exit_code, _files = _run_execute_review_pipeline(tmp_path)

    review_mock.assert_called_once()
    assert exit_code == 0


def test_dispatch_pipeline_attributes_phases_not_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_pipeline binds the live phase so dispatch-path token usage lands in
    execute/review, not the 'unknown' bucket that U1's read-side fix falls back to."""
    from agent_fleet.observability.context import bind_run
    from agent_fleet.observability.log import RunLog

    run_log = RunLog.create(run_id="attrib-test", include_memory_ring=False)

    def _execute(**_k: object) -> dict[str, object]:
        run_log.llm_usage(phase=None, model="m", duration_s=0.1, output_tokens=7)
        return {"phase": "execute", "stdout": "done", "exit_code": 0, "stderr": ""}

    def _review(**_k: object) -> dict[str, object]:
        run_log.llm_usage(phase=None, model="m", duration_s=0.1, output_tokens=11)
        return {
            "phase": "review",
            "verdict": "approve",
            "skipped": False,
            "passed": True,
            "exit_code": 0,
            "summary": "ok",
            "stdout": "",
            "stderr": "",
        }

    monkeypatch.setattr("agent_fleet.phases.run_execute_phase", _execute)
    monkeypatch.setattr(
        "agent_fleet.phases.run_scope_phase",
        lambda **_k: {"phase": "scope", "passed": True, "exit_code": 0},
    )
    monkeypatch.setattr(
        "agent_fleet.phases.run_verify_phases",
        lambda **_k: [{"phase": "verify", "passed": True, "exit_code": 0}],
    )
    monkeypatch.setattr("agent_fleet.phases.collect_changed_files", lambda _ws: ["a.py"])
    monkeypatch.setattr("agent_fleet.phases.changed_lines", lambda _ws: 500)
    monkeypatch.setattr("agent_fleet.phases.run_structured_review_phase", _review)

    with bind_run(run_log, run_log.context):
        _run_execute_review_pipeline(tmp_path)

    by_phase = run_log._usage_by_phase
    assert "execute" in by_phase
    assert "review" in by_phase
    assert "unknown" not in by_phase
