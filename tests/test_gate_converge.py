"""Tests for the gate's convergence loop — the decision the whole design rests on.

The gate does not cap fix rounds; it keeps fixing while the failing set
*strictly shrinks* and no new failures appear. These tests drive ``converge``
with a scripted sequence of head shas and test outcomes, so each stopping rule
is exercised without any real git or pytest.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 - built into concrete paths at runtime
from typing import Any

import pytest

from agent_fleet.gate import metrics as gm
from agent_fleet.gate.config import GateConfig
from agent_fleet.gate.pipeline import GatePipeline, TestRun
from agent_fleet.model_policy import ModelPolicy

_SHA = "a" * 40
_START = "b" * 40
_REF = None


@pytest.fixture(autouse=True)
def _no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """converge() creates real worktrees; stub them out for these tests."""
    monkeypatch.setattr("agent_fleet.gate.pipeline.prepare_worktree", lambda *_a, **_k: None)
    monkeypatch.setattr("agent_fleet.gate.pipeline.remove_worktree", lambda *_a, **_k: None)
    monkeypatch.setattr("agent_fleet.gate.pipeline.fetch_base", lambda *_a, **_k: None)


@dataclass
class _Script:
    """A scripted sequence of PR heads and the failing sets seen at each.

    ``converge`` calls the test runner once for the baseline, then once per fix
    round; it re-reads the PR head once per round, immediately after the fixer
    runs. So the two sequences are indexed separately: ``runs[0]`` is the
    baseline, ``runs[n]`` is round n's result, and ``heads[n-1]`` is the head
    the fixer pushed in round n.
    """

    heads: list[str]
    runs: list[TestRun]
    run_calls: int = 0
    head_calls: int = 0

    def next_head(self) -> str:
        index = min(self.head_calls, len(self.heads) - 1)
        self.head_calls += 1
        return self.heads[index]

    def next_run(self) -> TestRun:
        index = min(self.run_calls, len(self.runs) - 1)
        self.run_calls += 1
        return self.runs[index]


def _ref():  # noqa: ANN202
    from agent_fleet.gate.gitops import PullRequestRef

    return PullRequestRef(number=7, head_ref="fb/lane", head_sha=_START, state="OPEN")


def _run_converge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: _Script,
    *,
    max_fix_rounds: int = 4,
    untestable_open: bool = False,
    enable_fix: bool = True,
    backend_prompts: list[str] | None = None,
) -> tuple[str, gm.GateMetrics, GatePipeline]:
    """Drive one converge() against a scripted head/test sequence."""
    config = GateConfig(
        backend="cmd",
        model="m",
        judge_backend="cmd",
        judge_model="m",
        enable_judge=False,
        enable_fix=enable_fix,
        max_fix_rounds=max_fix_rounds,
        agent_timeout_s=5,
        test_timeout_s=5,
    )
    prompts: list[str] = backend_prompts if backend_prompts is not None else []

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
        {"id": "c-1", "source": "lens", "claim": "defect", "test_file": "tests/test_gate_c_1.py"}
    )
    if untestable_open:
        pipe.evidence.confirmed.append(
            {"id": "u-1", "source": "judge-untestable", "claim": "real", "test_file": None}
        )

    monkeypatch.setattr("agent_fleet.gate.pipeline.current_pr_head", lambda *_a: script.next_head())
    monkeypatch.setattr(
        "agent_fleet.gate.pipeline.GateTestRunner.run",
        lambda _self, _files: script.next_run(),
    )
    head, metric = pipe.converge(ref=_ref(), pr_tests=["tests/test_pr.py"])
    return head, metric, pipe


# ---------------------------------------------------------------------------
# Stopping rule: zero failing is approval
# ---------------------------------------------------------------------------


def test_converges_when_the_failing_set_reaches_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """5 failing -> 0 in one round: APPROVED, and the loop stops there."""
    script = _Script(
        heads=[_SHA],
        runs=[
            TestRun(failing=[f"t{i}" for i in range(5)], ran=1, tests_failed=True),
            TestRun(failing=[], ran=1, tests_failed=False),
        ],
    )
    _head, metric, _pipe = _run_converge(tmp_path, monkeypatch, script)
    assert metric.outcome == gm.OUTCOME_CONVERGED
    assert metric.failing_by_round == [5, 0]
    assert metric.round_count == 2  # baseline + one fix round


def test_already_green_needs_no_fix_round(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing failing at head: the gate approves without spending a fixer."""
    prompts: list[str] = []
    script = _Script(heads=[_SHA], runs=[TestRun(failing=[], ran=1)])
    _head, metric, _ = _run_converge(tmp_path, monkeypatch, script, backend_prompts=prompts)
    assert metric.outcome == gm.OUTCOME_CONVERGED
    assert metric.failing_by_round == [0]
    assert prompts == []  # no fixer was dispatched


# ---------------------------------------------------------------------------
# Stopping rule: continue while the set strictly shrinks
# ---------------------------------------------------------------------------


def test_continues_across_multiple_shrinking_rounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """5 -> 3 -> 1 -> 0 is three real rounds of progress, not a stall."""
    script = _Script(
        heads=["c" * 40, "d" * 40, "e" * 40],
        runs=[
            TestRun(failing=[f"t{i}" for i in range(5)], ran=1, tests_failed=True),
            TestRun(failing=["t0", "t1", "t2"], ran=1, tests_failed=True),
            TestRun(failing=["t0"], ran=1, tests_failed=True),
            TestRun(failing=[], ran=1),
        ],
    )
    _head, metric, _ = _run_converge(tmp_path, monkeypatch, script, max_fix_rounds=6)
    assert metric.outcome == gm.OUTCOME_CONVERGED
    assert metric.failing_by_round == [5, 3, 1, 0]
    rounds = [r for r in metric.rounds if r.round > 0]
    assert [r.fixed for r in rounds] == [2, 2, 1]
    assert all(r.progressed for r in rounds)


# ---------------------------------------------------------------------------
# Stopping rule: no progress is a stall, whatever the round count
# ---------------------------------------------------------------------------


def test_stalls_when_nothing_was_fixed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fixer that pushes but fixes nothing is a stall after one round."""
    script = _Script(
        heads=[_SHA],
        runs=[
            TestRun(failing=["t0", "t1"], ran=1, tests_failed=True),
            TestRun(failing=["t0", "t1"], ran=1, tests_failed=True),
        ],
    )
    _head, metric, _ = _run_converge(tmp_path, monkeypatch, script)
    assert metric.outcome == gm.OUTCOME_STALLED
    assert metric.failing_by_round == [2, 2]
    assert metric.round_count == 2


def test_stalls_when_the_fixing_round_introduces_a_new_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Net progress is not progress if something new broke: stop and escalate."""
    script = _Script(
        heads=[_SHA],
        runs=[
            TestRun(failing=["t0", "t1"], ran=1, tests_failed=True),
            TestRun(failing=["t0", "brand_new"], ran=1, tests_failed=True),
        ],
    )
    _head, metric, _ = _run_converge(tmp_path, monkeypatch, script)
    assert metric.outcome == gm.OUTCOME_STALLED
    last = metric.rounds[-1]
    assert last.new_failures == 1
    assert not last.progressed


def test_stalls_when_the_failing_set_does_not_shrink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _Script(
        heads=[_SHA],
        runs=[
            TestRun(failing=["t0", "t1"], ran=1, tests_failed=True),
            TestRun(failing=["t0", "t1", "t2"], ran=1, tests_failed=True),
        ],
    )
    _head, metric, _ = _run_converge(tmp_path, monkeypatch, script)
    assert metric.outcome == gm.OUTCOME_STALLED


# ---------------------------------------------------------------------------
# max_fix_rounds is only a safety net
# ---------------------------------------------------------------------------


def test_safety_net_stops_a_run_that_keeps_shrinking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One-test-per-round progress never converges; the cap must still fire."""
    script = _Script(
        heads=["c" * 40, "d" * 40, "e" * 40],
        runs=[
            TestRun(failing=["t0", "t1", "t2", "t3"], ran=1, tests_failed=True),
            TestRun(failing=["t0", "t1", "t2"], ran=1, tests_failed=True),
            TestRun(failing=["t0", "t1"], ran=1, tests_failed=True),
            TestRun(failing=["t0"], ran=1, tests_failed=True),
        ],
    )
    _head, metric, _ = _run_converge(tmp_path, monkeypatch, script, max_fix_rounds=3)
    assert metric.outcome == gm.OUTCOME_CAP
    # Baseline + exactly max_fix_rounds fix rounds, never more.
    assert metric.round_count == 4
    # It was still making real progress when the net bound.
    assert all(r.progressed for r in metric.rounds if r.round > 0)


# ---------------------------------------------------------------------------
# Terminal failure modes
# ---------------------------------------------------------------------------


def test_no_push_ends_the_run_without_another_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fixer that committed nothing cannot be re-asked; escalate."""
    script = _Script(
        heads=[_START],  # the fixer pushed nothing
        runs=[TestRun(failing=["t0"], ran=1, tests_failed=True)],
    )
    _head, metric, _ = _run_converge(tmp_path, monkeypatch, script)
    assert metric.outcome == gm.OUTCOME_NO_PUSH
    assert metric.round_count == 1  # baseline only


def test_broken_tests_at_the_new_head_stop_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the suite cannot run at the pushed head we learn nothing about the
    fix: record it and stop rather than reading a broken harness as progress."""
    script = _Script(
        heads=[_SHA],
        runs=[
            TestRun(failing=["t0"], ran=1, tests_failed=True),
            TestRun(infra_error="collection error", ran=1),
        ],
    )
    _head, metric, _ = _run_converge(tmp_path, monkeypatch, script)
    assert metric.outcome == gm.OUTCOME_TESTS_BROKEN
    assert metric.round_count == 2  # baseline + the round that broke the suite


def test_broken_tests_at_the_start_head_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A baseline that cannot run means the gate has no evidence at all."""
    from agent_fleet.gate.pipeline import GateInfraError

    script = _Script(
        heads=[_SHA],
        runs=[TestRun(infra_error="collection error", ran=1)],
    )
    with pytest.raises(GateInfraError, match="collection error"):
        _run_converge(tmp_path, monkeypatch, script)


def test_fix_disabled_returns_the_start_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts: list[str] = []
    script = _Script(heads=[_SHA], runs=[TestRun(failing=["t0"], ran=1, tests_failed=True)])
    head, metric, _ = _run_converge(
        tmp_path, monkeypatch, script, enable_fix=False, backend_prompts=prompts
    )
    assert head == _START
    assert prompts == []
    assert metric.outcome == gm.OUTCOME_CAP


# ---------------------------------------------------------------------------
# The fix prompt names the tests failing NOW
# ---------------------------------------------------------------------------


def test_fix_prompt_lists_the_current_failures_and_keeps_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fixer must be told what is red now and what must stay green."""
    prompts: list[str] = []
    script = _Script(
        heads=[_SHA],
        runs=[
            TestRun(
                failing=["tests/test_gate_c_1.py::test_x", "tests/test_pr.py::test_y"],
                ran=1,
                tests_failed=True,
            ),
            TestRun(failing=["tests/test_gate_c_1.py::test_x"], ran=1, tests_failed=True),
        ],
    )
    _run_converge(tmp_path, monkeypatch, script, backend_prompts=prompts)
    fix_prompt = next(p for p in prompts if "Fix round 1" in p)
    assert "tests/test_pr.py::test_y" in fix_prompt
    assert "must keep passing" in fix_prompt
    assert "never weaken their assertions" in fix_prompt


# ---------------------------------------------------------------------------
# Untestable claims: the judge recheck gates approval
# ---------------------------------------------------------------------------


def test_untestable_open_gets_one_fix_round_then_escalates_if_still_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tests green, only untestable blockers left: fix them once, then escalate.

    The round is bounded at one by construction — with no test to go green there
    is no measurable progress to converge on, only the judge's yes/no.
    """
    prompts: list[str] = []
    script = _Script(
        heads=[_SHA],
        runs=[TestRun(failing=[], ran=1), TestRun(failing=[], ran=1)],
    )
    monkeypatch.setattr(GatePipeline, "recheck_untestable", lambda *_a, **_k: False)
    head, metric, _ = _run_converge(
        tmp_path, monkeypatch, script, untestable_open=True, backend_prompts=prompts
    )
    assert metric.outcome == gm.OUTCOME_UNTESTABLE_NEEDS_REVIEW
    assert metric.untestable_real == 1
    assert metric.round_count == 2  # baseline + the one untestable round
    assert len(prompts) == 1
    assert head == _SHA


def test_untestable_only_never_reaches_the_recheck_without_a_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fixer that pushed nothing changed no head, so there is nothing to recheck."""
    calls: list[bool] = []
    monkeypatch.setattr(
        GatePipeline,
        "recheck_untestable",
        lambda *_a, **_k: calls.append(True) or False,
    )
    script = _Script(heads=[_START], runs=[TestRun(failing=[], ran=1)])
    _head, metric, _ = _run_converge(tmp_path, monkeypatch, script, untestable_open=True)
    assert calls == []
    assert metric.outcome == gm.OUTCOME_UNTESTABLE_NEEDS_REVIEW


def test_untestable_resolved_after_a_failing_round_approves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real failing round still runs, and the recheck then gates approval."""
    script = _Script(
        heads=[_SHA],
        runs=[TestRun(failing=["t0"], ran=1, tests_failed=True), TestRun(failing=[], ran=1)],
    )
    monkeypatch.setattr(GatePipeline, "recheck_untestable", lambda *_a, **_k: True)
    _head, metric, _ = _run_converge(tmp_path, monkeypatch, script, untestable_open=True)
    assert metric.outcome == gm.OUTCOME_CONVERGED


def test_untestable_still_unresolved_blocks_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _Script(
        heads=[_SHA],
        runs=[TestRun(failing=["t0"], ran=1, tests_failed=True), TestRun(failing=[], ran=1)],
    )
    monkeypatch.setattr(GatePipeline, "recheck_untestable", lambda *_a, **_k: False)
    _head, metric, _ = _run_converge(tmp_path, monkeypatch, script, untestable_open=True)
    assert metric.outcome == gm.OUTCOME_UNTESTABLE_UNRESOLVED
