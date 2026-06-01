"""Tests for the Run Controller seam (C2).

Table-driven tests for ThresholdController.before_fix (pure) plus a
runner-level integration test verifying that a FIX-spiral with ratio > 0.6
halts and produces a salvage draft PR.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agent_fleet.contracts.implementation_brief import ImplementationBrief
from agent_fleet.contracts.task_spec import (
    DecompositionDecision,
    RiskTier,
    Scope,
    TaskSpec,
)
from agent_fleet.contracts.verify_result import VerifyResult, VerifySeverity
from agent_fleet.disposition import (
    DispositionKind,
    DispositionPolicy,
    RunFacts,
    decide_disposition,
)
from agent_fleet.level_up.models import DispatchEquip
from agent_fleet.run_controller import (
    ControlDecision,
    ControllerPolicy,
    RunMetrics,
    ThresholdController,
    _build_run_metrics,
)
from agent_fleet.runner import LocalFleetRunner

# ---------------------------------------------------------------------------
# Helper: build a usage_rollup dict that matches the RunLog snapshot shape
# ---------------------------------------------------------------------------

def _usage_rollup(
    *,
    total_tokens: int,
    fix_tokens: int,
) -> dict[str, Any]:
    impl_tokens = max(0, total_tokens - fix_tokens)
    by_phase: dict[str, Any] = {
        "IMPLEMENT": {"total_tokens": impl_tokens, "calls": 1},
    }
    if fix_tokens > 0:
        by_phase["FIX"] = {"total_tokens": fix_tokens, "calls": 1}
    return {
        "calls": 2,
        "duration_s": 1.0,
        "totals": {"total_tokens": total_tokens},
        "by_phase": by_phase,
        "changed_lines": 0,
        "tokens_per_changed_line": 0,
    }


# ---------------------------------------------------------------------------
# Pure table tests: ThresholdController.before_fix -> ControlDecision
# ---------------------------------------------------------------------------

_DEFAULT_POLICY = ControllerPolicy()

_CTRL = ThresholdController()


@pytest.mark.parametrize(
    ("metrics", "policy", "expected"),
    [
        pytest.param(
            RunMetrics(
                verify_attempts=1,
                fix_token_total=0,
                total_tokens=1000,
                fix_phase_ratio=0.0,
                cost_alerts=(),
            ),
            _DEFAULT_POLICY,
            ControlDecision.CONTINUE,
            id="first_attempt_no_ratio_continues",
        ),
        pytest.param(
            RunMetrics(
                verify_attempts=2,
                fix_token_total=700,
                total_tokens=1000,
                fix_phase_ratio=0.7,
                cost_alerts=("fix_phase_token_ratio_high",),
            ),
            _DEFAULT_POLICY,
            ControlDecision.HALT,
            id="high_ratio_after_2_attempts_halts",
        ),
        pytest.param(
            RunMetrics(
                verify_attempts=3,
                fix_token_total=400,
                total_tokens=1000,
                fix_phase_ratio=0.4,
                cost_alerts=(),
            ),
            _DEFAULT_POLICY,
            ControlDecision.HALT,
            id="attempt_ceiling_reached_halts",
        ),
        pytest.param(
            RunMetrics(
                verify_attempts=2,
                fix_token_total=600,
                total_tokens=1000,
                fix_phase_ratio=0.6,
                cost_alerts=("fix_phase_token_ratio_high",),
            ),
            _DEFAULT_POLICY,
            ControlDecision.HALT,
            id="alert_fires_halts_when_halt_on_alert_true",
        ),
        pytest.param(
            RunMetrics(
                verify_attempts=2,
                fix_token_total=600,
                total_tokens=1000,
                fix_phase_ratio=0.6,
                cost_alerts=("fix_phase_token_ratio_high",),
            ),
            ControllerPolicy(halt_on_alert=False),
            ControlDecision.CONTINUE,
            id="alert_fires_but_halt_on_alert_false_continues",
        ),
        pytest.param(
            RunMetrics(
                verify_attempts=2,
                fix_token_total=550,
                total_tokens=1000,
                fix_phase_ratio=0.55,
                cost_alerts=(),
            ),
            _DEFAULT_POLICY,
            ControlDecision.CONTINUE,
            id="ratio_below_threshold_continues",
        ),
        pytest.param(
            RunMetrics(
                verify_attempts=1,
                fix_token_total=700,
                total_tokens=1000,
                fix_phase_ratio=0.7,
                cost_alerts=("fix_phase_token_ratio_high",),
            ),
            ControllerPolicy(halt_on_alert=False),
            ControlDecision.CONTINUE,
            id="high_ratio_first_attempt_no_alert_gate_continues",
        ),
        pytest.param(
            RunMetrics(
                verify_attempts=5,
                fix_token_total=200,
                total_tokens=1000,
                fix_phase_ratio=0.2,
                cost_alerts=(),
            ),
            ControllerPolicy(halt_after_attempts=3),
            ControlDecision.HALT,
            id="custom_ceiling_halts_when_exceeded",
        ),
        pytest.param(
            RunMetrics(
                verify_attempts=2,
                fix_token_total=700,
                total_tokens=1000,
                fix_phase_ratio=0.7,
                cost_alerts=(),
            ),
            ControllerPolicy(max_fix_ratio=0.8),
            ControlDecision.CONTINUE,
            id="custom_ratio_ceiling_not_exceeded_continues",
        ),
    ],
)
def test_threshold_controller(
    metrics: RunMetrics, policy: ControllerPolicy, expected: ControlDecision
) -> None:
    assert _CTRL.before_fix(metrics, policy) == expected


# ---------------------------------------------------------------------------
# _build_run_metrics helper
# ---------------------------------------------------------------------------

def test_build_run_metrics_computes_ratio() -> None:
    rollup = _usage_rollup(total_tokens=1000, fix_tokens=700)
    m = _build_run_metrics(rollup, verify_attempts=3)
    assert m.verify_attempts == 3
    assert abs(m.fix_phase_ratio - 0.7) < 0.01
    assert m.fix_token_total == 700
    assert m.total_tokens == 1000


def test_build_run_metrics_no_rollup() -> None:
    m = _build_run_metrics(None, verify_attempts=1)
    assert m.fix_phase_ratio == 0.0
    assert m.total_tokens == 0
    assert m.cost_alerts == ()


# ---------------------------------------------------------------------------
# Runner-level integration: FIX spiral halts and produces a salvage draft PR
# ---------------------------------------------------------------------------


class _FakeGitOps:
    def __init__(self) -> None:
        self.pushed: list[str] = []

    def push_branch(self, worktree: Path, branch_name: str) -> None:
        del worktree
        self.pushed.append(branch_name)

    def setup_workspace(self, *_a: object, **_k: object) -> Path:
        return Path("/tmp/wt")

    def teardown_workspace(self, *_a: object, **_k: object) -> None:
        pass

    def create_branch(self, *_a: object, **_k: object) -> None:
        pass

    def commit_changes(self, *_a: object, **_k: object) -> str | None:
        return None

    def changed_files(self, *_a: object, **_k: object) -> list[Path]:
        return [Path("src/foo.py")]

    def diff_summary(self, *_a: object, **_k: object) -> str:
        return "diff --git a/src/foo.py"


class _FakeForge:
    def __init__(self, pr_number: int = 42) -> None:
        self._pr_number = pr_number
        self.open_pr_calls: list[dict[str, Any]] = []
        self.comments: list[tuple[int, str]] = []

    def open_pr(self, **kwargs: object) -> int:
        self.open_pr_calls.append(dict(kwargs))
        return self._pr_number

    def mark_ready(self, pr_number: int) -> None:
        del pr_number

    def comment(self, issue_or_pr: int, body: str) -> None:
        self.comments.append((issue_or_pr, body))

    def get_labels(self, issue_or_pr: int) -> list[str]:
        del issue_or_pr
        return []



class _ImmediateHaltController:
    """Controller that always returns HALT on first before_fix call."""

    def before_fix(self, m: RunMetrics, policy: ControllerPolicy) -> ControlDecision:
        del m, policy
        return ControlDecision.HALT


def _minimal_task_spec() -> TaskSpec:
    return TaskSpec(
        issue_number=1,
        decomposition_decision=DecompositionDecision.SINGLE,
        decomposition_reason="ok",
        child_issues_proposed=[],
        scope=Scope(allowed_paths=["src/"], forbidden_paths=[]),
        research_plan=[],
        acceptance_criteria=["pass"],
        risk_tier=RiskTier.LOW,
        critical_paths_touched=[],
        coordination_spec=None,
    )


def _minimal_brief() -> ImplementationBrief:
    return ImplementationBrief(
        issue_number=1,
        summary="stub",
        files_to_create=[],
        files_to_modify=["src/foo.py"],
        test_strategy="none",
        acceptance_criteria=["pass"],
        references=[],
    )


def _retry_verify_result() -> VerifyResult:
    return VerifyResult(
        severity=VerifySeverity.RETRY,
        checks=[],
        violating_paths=[],
        files_changed=["src/foo.py"],
        message="test failure",
    )


def test_controller_halt_produces_salvage_draft_pr(tmp_path: Path) -> None:
    """The runner sets _halted_by_controller when the controller returns HALT
    and routes to controller_halted_salvaged disposition.

    Exercises the actual verify/fix loop code path — controller.before_fix() is
    called by the runner, which then breaks with _halted_by_controller=True.
    """
    forge = _FakeForge(pr_number=77)
    verifier = MagicMock()
    verifier.check.return_value = _retry_verify_result()

    equip = DispatchEquip(
        skill_slots_execute=(),
        skill_slots_review=(),
        level_up_generation=0,
        compose_body="",
    )

    patches = [
        patch("agent_fleet.runner.plan", return_value=_minimal_task_spec()),
        patch("agent_fleet.runner.research_all", return_value=[]),
        patch("agent_fleet.runner.synthesize", return_value=_minimal_brief()),
        patch("agent_fleet.runner.implement"),
        patch("agent_fleet.runner.coerce_empty_decompose", side_effect=lambda ts: (ts, False)),
        patch("agent_fleet.runner.resolve_dispatch_equip", return_value=equip),
        patch("agent_fleet.runner.find_repo_config", return_value=None),
        patch("agent_fleet.runner.load_fleet_config", return_value=MagicMock(mcp_servers={})),
        patch("agent_fleet.runner.create_fleet_session", return_value=None),
        patch("agent_fleet.runner.get_changed_files", return_value=["src/foo.py"]),
        patch("agent_fleet.observability.log._DEFAULT_RUNS_DIR", tmp_path),
    ]

    runner = LocalFleetRunner(
        backend=MagicMock(),
        persona_resolver=MagicMock(),
        git_ops=_FakeGitOps(),
        verifier=verifier,
        forge=forge,
        controller=_ImmediateHaltController(),
    )

    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)

        result = runner.run(
            task_id=1,
            title="test task",
            body="fix something",
            persona="coder",
            repo_root=tmp_path,
            base_branch="main",
            pr_title="WIP fix",
            pr_labels=["fleet-draft"],
        )

    assert result.outcome == "controller_halted_salvaged"
    assert len(forge.open_pr_calls) == 1
    call = forge.open_pr_calls[0]
    assert call["draft"] is True
    assert "fleet-salvage" in call["labels"]
    assert result.pr_number == 77


def test_halted_by_controller_flag_in_disposition() -> None:
    """halted_by_controller=True routes to controller_halted_salvaged even
    when salvage_on_verify_failed=False would otherwise block it.
    """
    facts = RunFacts(
        verify_ok=False,
        verify_fatal=False,
        scope_violated=False,
        changed_files=("src/foo.py",),
        halted_by_controller=True,
    )
    policy = DispositionPolicy(salvage_on_verify_failed=False)
    disp = decide_disposition(facts, policy)

    assert disp.kind == DispositionKind.SALVAGE
    assert disp.outcome == "controller_halted_salvaged"
    assert disp.draft is True


def test_halted_by_controller_false_falls_through_normal_path() -> None:
    """When halted_by_controller=False, disposition is unchanged from C1."""
    facts = RunFacts(
        verify_ok=False,
        verify_fatal=False,
        scope_violated=False,
        changed_files=("src/foo.py",),
        halted_by_controller=False,
    )
    policy = DispositionPolicy()
    disp = decide_disposition(facts, policy)

    assert disp.kind == DispositionKind.SALVAGE
    assert disp.outcome == "verify_failed_salvaged"
