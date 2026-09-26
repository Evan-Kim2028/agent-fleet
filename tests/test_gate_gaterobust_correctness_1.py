"""A green suite with an untestable blocker must not let a fix round ship a red test.

The untestable fix round exists so a judge-confirmed defect that no local test
can express (a docs contradiction, a script's behaviour) still gets fixed. But
that round also runs against a test set that was green a moment earlier, and the
fixer is free to break something on the way past.

The recheck judge only ever rules on the untestable claims: it is never shown
the failing test the round introduced, and it is not the mechanism that decides
whether the deterministic half of the run is green. So the test run itself has
to be authoritative, and it has to be authoritative at the new head -- not only
at the baseline that was green before the fixer was dispatched.

This pins that: when the round leaves a test failing, the run is not converged,
whatever the recheck judge says about the untestable blocker.
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
REGRESSED = "tests/test_regressed.py::test_x"


@pytest.fixture(autouse=True)
def _no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """converge() creates real worktrees; stub them out for this test."""
    monkeypatch.setattr("agent_fleet.gate.pipeline.prepare_worktree", lambda *_a, **_k: None)
    monkeypatch.setattr("agent_fleet.gate.pipeline.remove_worktree", lambda *_a, **_k: None)
    monkeypatch.setattr("agent_fleet.gate.pipeline.fetch_base", lambda *_a, **_k: None)


def _ref() -> Any:  # noqa: ANN401
    from agent_fleet.gate.gitops import PullRequestRef

    return PullRequestRef(number=7, head_ref="fb/lane", head_sha=_START, state="OPEN")


def _converge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runs: list[TestRun], head: str
) -> Any:  # noqa: ANN401
    """Drive converge(): green baseline, one open untestable blocker, one pushed round."""
    config = GateConfig(
        backend="cmd",
        model="m",
        judge_backend="cmd",
        judge_model="m",
        enable_judge=False,
        enable_fix=True,
        max_fix_rounds=4,
        lens_timeout_s=5,
        verify_timeout_s=5,
        judge_timeout_s=5,
        fix_timeout_s=5,
        test_timeout_s=5,
    )

    class _Backend:
        def run(self, *_a: Any, **_kw: Any) -> Any:  # noqa: ANN401
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
        {
            "id": "u-1",
            "source": "judge-untestable",
            "claim": UNTESTABLE_CLAIM,
            "test_file": None,
        }
    )

    scripted = list(runs)

    def _next_run(_self: Any, _files: Any) -> TestRun:  # noqa: ANN401
        return scripted.pop(0) if len(scripted) > 1 else scripted[0]

    monkeypatch.setattr("agent_fleet.gate.pipeline.current_pr_head", lambda *_a, **_k: head)
    monkeypatch.setattr(
        "agent_fleet.gate.pipeline.GateTestRunner.run", lambda s, _f: _next_run(s, _f)
    )
    # The recheck judge rules only on the untestable claim, and says it is fixed.
    monkeypatch.setattr(GatePipeline, "recheck_untestable", lambda *_a, **_k: True)
    return pipe.converge(ref=_ref(), pr_tests=[])


def test_untestable_fix_round_may_not_ship_a_failing_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fixer regressed a test on its way to fixing the untestable blocker.

    Baseline green, pushed head red. run() maps OUTCOME_CONVERGED to
    GateOutcome.APPROVED, so converging here merges a failing test.
    """
    _head, metric = _converge(
        tmp_path,
        monkeypatch,
        runs=[
            TestRun(failing=[], ran=1),  # baseline: green, plus an untestable blocker
            TestRun(failing=[REGRESSED], ran=1, tests_failed=True),  # the round regressed one
        ],
        head=_PUSHED,
    )

    # The failing test was really observed at the head we are about to hand back.
    assert metric.failing_by_round[-1] == 1

    # So the run must not be reported as converged -- the recheck judge's yes on
    # the untestable claim says nothing about this test.
    assert metric.outcome != gm.OUTCOME_CONVERGED, (
        f"converged on a head with a failing test: outcome={metric.outcome} "
        f"failing_by_round={metric.failing_by_round}"
    )
