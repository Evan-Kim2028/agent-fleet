"""Tests for untestable blockers getting a fix round instead of an immediate escalation.

A judge-confirmed blocker that no local test can express — a docs contradiction, a
script's behaviour — used to end the run the moment it was observed: the test set
was green, a fixer had nothing to make fail, and ``converge`` escalated without
dispatching one. The defect is real and confirmed, so it *can* be fixed; the gate
simply never gave anyone the job.

These tests pin the corrected shape: one fix round carrying the untestable list,
then the recheck judge decides. Fail-closed throughout — an unresolved blocker
still escalates, and it takes the recheck to say "fixed", never the fixer's own
report.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 - built into concrete paths at runtime
from typing import Any

import pytest

from agent_fleet.gate import metrics as gm
from agent_fleet.gate.config import GateConfig
from agent_fleet.gate.pipeline import GatePipeline, TestRun
from agent_fleet.model_policy import ModelPolicy

_START = "b" * 40
_PUSHED = "a" * 40

UNTESTABLE_CLAIM = "docs contradict the shipped behaviour: OPTIONS says --limit 10, code says 40"


@pytest.fixture(autouse=True)
def _no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """converge() creates real worktrees; stub them out for these tests."""
    monkeypatch.setattr("agent_fleet.gate.pipeline.prepare_worktree", lambda *_a, **_k: None)
    monkeypatch.setattr("agent_fleet.gate.pipeline.remove_worktree", lambda *_a, **_k: None)
    monkeypatch.setattr("agent_fleet.gate.pipeline.fetch_base", lambda *_a, **_k: None)


class _Script:
    """The head shas and test results ``converge`` sees, one entry per call."""

    def __init__(self, heads: list[str], runs: list[TestRun]) -> None:
        self.heads = heads
        self.runs = runs
        self.run_calls = 0
        self.head_calls = 0

    def next_head(self) -> str:
        index = min(self.head_calls, len(self.heads) - 1)
        self.head_calls += 1
        return self.heads[index]

    def next_run(self) -> TestRun:
        index = min(self.run_calls, len(self.runs) - 1)
        self.run_calls += 1
        return self.runs[index]


def _ref() -> Any:  # noqa: ANN401
    from agent_fleet.gate.gitops import PullRequestRef

    return PullRequestRef(number=7, head_ref="fb/lane", head_sha=_START, state="OPEN")


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: _Script,
    *,
    enable_fix: bool = True,
    max_fix_rounds: int = 4,
) -> tuple[str, gm.GateMetrics, list[str]]:
    """Drive converge() with one open untestable blocker; return the prompts sent."""
    config = GateConfig(
        backend="cmd",
        model="m",
        judge_backend="cmd",
        judge_model="m",
        enable_judge=False,
        enable_fix=enable_fix,
        max_fix_rounds=max_fix_rounds,
        lens_timeout_s=5,
        verify_timeout_s=5,
        judge_timeout_s=5,
        fix_timeout_s=5,
        test_timeout_s=5,
    )
    prompts: list[str] = []

    class _Backend:
        def run(self, prompt: str, **_kw: Any) -> Any:  # noqa: ANN401
            prompts.append(prompt)
            return type("R", (), {"exit_code": 0, "stdout": "", "stderr": ""})()

    pipe = GatePipeline(
        repo=tmp_path / "repo",
        pr_number=7,
        config=config,
        policy=ModelPolicy(backends={}),
        backend=_Backend(),  # type: ignore[arg-type]
        gate_dir=tmp_path / "gate",
        use_systemd=False,
    )
    pipe.evidence.confirmed.append(
        {"id": "u-1", "source": "judge-untestable", "claim": UNTESTABLE_CLAIM, "test_file": None}
    )

    monkeypatch.setattr("agent_fleet.gate.pipeline.current_pr_head", lambda *_a: script.next_head())
    monkeypatch.setattr(
        "agent_fleet.gate.pipeline.GateTestRunner.run",
        lambda _self, _files: script.next_run(),
    )
    head, metric = pipe.converge(ref=_ref(), pr_tests=[])
    return head, metric, prompts


# ---------------------------------------------------------------------------
# A confirmed untestable defect is fixable work, so a fixer must be dispatched
# ---------------------------------------------------------------------------


def test_green_tests_with_untestable_blockers_dispatch_a_fix_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug: the untestable list never reached a fixer, so nothing was fixed."""
    script = _Script(heads=[_PUSHED], runs=[TestRun(failing=[], ran=1), TestRun(failing=[], ran=1)])
    monkeypatch.setattr(GatePipeline, "recheck_untestable", lambda *_a, **_k: True)
    _head, _metric, prompts = _run(tmp_path, monkeypatch, script)

    assert len(prompts) == 1
    fix = prompts[0]
    assert "Fix PR #7" in fix
    assert UNTESTABLE_CLAIM in fix
    assert "resolve these confirmed (untestable) defects" in fix


def test_untestable_round_runs_once_however_high_the_fix_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One round is the whole mechanism; a raised cap must not turn it into a loop."""
    rechecks: list[bool] = []
    monkeypatch.setattr(
        GatePipeline,
        "recheck_untestable",
        lambda *_a, **_k: rechecks.append(True) or True,
    )
    script = _Script(
        heads=[_PUSHED, "c" * 40, "d" * 40],
        runs=[TestRun(failing=[], ran=1)] * 6,
    )
    _head, _metric, prompts = _run(tmp_path, monkeypatch, script, max_fix_rounds=4)

    assert len(prompts) == 1
    assert len(rechecks) == 1


# ---------------------------------------------------------------------------
# The recheck judge still decides; the fixer never approves by itself
# ---------------------------------------------------------------------------


def test_untestable_resolved_by_the_fix_round_approves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _Script(heads=[_PUSHED], runs=[TestRun(failing=[], ran=1), TestRun(failing=[], ran=1)])
    monkeypatch.setattr(GatePipeline, "recheck_untestable", lambda *_a, **_k: True)
    head, metric, _prompts = _run(tmp_path, monkeypatch, script)
    assert metric.outcome == gm.OUTCOME_CONVERGED
    assert head == _PUSHED


def test_untestable_still_unresolved_keeps_needing_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fixer that pushes a change does not get to call its own blocker fixed."""
    script = _Script(heads=[_PUSHED], runs=[TestRun(failing=[], ran=1), TestRun(failing=[], ran=1)])
    monkeypatch.setattr(GatePipeline, "recheck_untestable", lambda *_a, **_k: False)
    _head, metric, _prompts = _run(tmp_path, monkeypatch, script)
    assert metric.outcome == gm.OUTCOME_UNTESTABLE_NEEDS_REVIEW
    assert metric.untestable_real == 1


def test_untestable_fix_still_fails_closed_when_the_recheck_dies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A judge that could not answer is not evidence of resolution."""
    script = _Script(heads=[_PUSHED], runs=[TestRun(failing=[], ran=1), TestRun(failing=[], ran=1)])
    monkeypatch.setattr(GatePipeline, "recheck_untestable", lambda *_a, **_k: False)
    _head, metric, _prompts = _run(tmp_path, monkeypatch, script)
    assert metric.outcome != gm.OUTCOME_CONVERGED


def test_untestable_round_that_pushes_nothing_escalates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No push means no fix was made; the blocker is still open."""
    script = _Script(heads=[_START], runs=[TestRun(failing=[], ran=1)])
    monkeypatch.setattr(GatePipeline, "recheck_untestable", lambda *_a, **_k: True)
    _head, metric, _prompts = _run(tmp_path, monkeypatch, script)
    assert metric.outcome == gm.OUTCOME_UNTESTABLE_NEEDS_REVIEW
    assert metric.round_count == 1  # baseline only: the fixer never pushed


# ---------------------------------------------------------------------------
# enable_fix: false still means "do not dispatch a fixer"
# ---------------------------------------------------------------------------


def test_enable_fix_false_escalates_untestable_without_a_fixer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _Script(heads=[_PUSHED], runs=[TestRun(failing=[], ran=1)])
    _head, metric, prompts = _run(tmp_path, monkeypatch, script, enable_fix=False)
    assert prompts == []
    assert metric.outcome == gm.OUTCOME_CAP
