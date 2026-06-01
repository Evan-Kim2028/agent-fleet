"""Unit tests for silphco.selfimprove.gate — regression gate logic.

Uses a fake promptfoo runner to test threshold enforcement without requiring
the real promptfoo CLI.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from silphco.selfimprove.gate import (
    EvalStats,
    GateEvalError,
    GatePreconditionError,
    GateResult,
    _infer_category,
    _parse_results,
    run_gate,
)
from silphco.selfimprove.mine import ErrorClass, FailureSignature
from silphco.selfimprove.propose import ChangeProposal


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SIG = FailureSignature(
    persona="backend",
    phase="verify",
    error_class=ErrorClass.SCHEMA_VALIDATION_FAILED,
)

_PROPOSAL = ChangeProposal(
    signature=_SIG,
    target_file="agents/personas/backend.md",
    rationale="Root cause: missing severity key hint.",
    diff=(
        "--- a/agents/personas/backend.md\n"
        "+++ b/agents/personas/backend.md\n"
        "@@ -5,0 +6,1 @@\n"
        "+Always include `severity` in VerifyResult.\n"
    ),
    raw_llm_output="",
)


def _make_promptfoo_output(
    *,
    frozen_total: int = 2,
    frozen_passed: int = 2,
    target_total: int = 2,
    target_passed: int = 1,
) -> str:
    """Build a fake promptfoo JSON output."""
    results = []
    for i in range(frozen_total):
        results.append({
            "description": f"[frozen_success] test {i}",
            "success": i < frozen_passed,
            "vars": {"category": "frozen_success"},
        })
    for i in range(target_total):
        results.append({
            "description": f"[target_signature] test {i}",
            "success": i < target_passed,
            "vars": {"category": "target_signature"},
        })
    return json.dumps({"results": {"results": results}})


# ---------------------------------------------------------------------------
# _parse_results
# ---------------------------------------------------------------------------

class TestParseResults:
    def test_parses_frozen_and_target_categories(self):
        output = _make_promptfoo_output(
            frozen_total=3, frozen_passed=3, target_total=2, target_passed=1
        )
        stats = _parse_results(output)
        assert stats["frozen_success"].total == 3
        assert stats["frozen_success"].passed == 3
        assert stats["target_signature"].total == 2
        assert stats["target_signature"].passed == 1

    def test_empty_json_returns_zero_stats(self):
        stats = _parse_results("{}")
        assert stats["frozen_success"] == EvalStats(0, 0)
        assert stats["target_signature"] == EvalStats(0, 0)

    def test_malformed_json_returns_zero_stats(self):
        stats = _parse_results("not-json")
        assert stats["frozen_success"] == EvalStats(0, 0)
        assert stats["target_signature"] == EvalStats(0, 0)

    def test_flat_results_list(self):
        # Some promptfoo versions return a flat list
        results = [
            {"description": "[frozen_success] t1", "success": True},
            {"description": "[target_signature] t2", "success": False},
        ]
        output = json.dumps({"results": results})
        stats = _parse_results(output)
        assert stats["frozen_success"].passed == 1
        assert stats["target_signature"].passed == 0

    def test_pass_rate_vacuously_passes_when_no_cases(self):
        stats = _parse_results("{}")
        assert stats["frozen_success"].pass_rate == 1.0
        assert stats["target_signature"].pass_rate == 1.0


# ---------------------------------------------------------------------------
# _infer_category
# ---------------------------------------------------------------------------

class TestInferCategory:
    def test_description_bracket_prefix(self):
        assert _infer_category("[frozen_success] some test", {}) == "frozen_success"
        assert _infer_category("[target_signature] some test", {}) == "target_signature"

    def test_falls_back_to_vars_category(self):
        assert _infer_category("no bracket", {"vars": {"category": "frozen_success"}}) == "frozen_success"

    def test_defaults_to_frozen_for_unknown(self):
        # Unknown categories are treated as frozen (conservative)
        assert _infer_category("unknown thing", {}) == "frozen_success"


# ---------------------------------------------------------------------------
# EvalStats.pass_rate
# ---------------------------------------------------------------------------

class TestEvalStats:
    def test_pass_rate_zero_total(self):
        assert EvalStats(total=0, passed=0).pass_rate == 1.0

    def test_pass_rate_all_pass(self):
        assert EvalStats(total=4, passed=4).pass_rate == pytest.approx(1.0)

    def test_pass_rate_half_pass(self):
        assert EvalStats(total=4, passed=2).pass_rate == pytest.approx(0.5)

    def test_pass_rate_none_pass(self):
        assert EvalStats(total=4, passed=0).pass_rate == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# run_gate — frozen-success regression logic with fake promptfoo
# ---------------------------------------------------------------------------

class TestRunGate:
    """Tests run_gate with a fake promptfoo runner patched in.

    We patch gate._run_promptfoo to avoid requiring the real CLI, and
    gate._apply_diff_to_scratch to avoid requiring the patch binary.
    """

    def _patch_apply_diff(self, tmp_path: Path, proposal: ChangeProposal):
        """Create the target file so _apply_diff_to_scratch can read it."""
        target = tmp_path / proposal.target_file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# Backend\n")

    def _run_gate_with_fake_promptfoo(
        self,
        tmp_path: Path,
        before_output: str,
        after_output: str,
        config: dict | None = None,
    ) -> "GateResult":
        """Helper: run_gate with _promptfoo_available=True and patched promptfoo."""
        with patch("silphco.selfimprove.gate._promptfoo_available", return_value=True), \
             patch("silphco.selfimprove.gate._run_promptfoo", side_effect=[
                 (before_output, "", 0),
                 (after_output, "", 0),
             ]), \
             patch("silphco.selfimprove.gate._apply_diff_to_scratch"):
            return run_gate(_PROPOSAL, repo_root=tmp_path, config=config)

    def test_gate_passes_when_no_regression_and_target_meets_min(self, tmp_path: Path):
        self._patch_apply_diff(tmp_path, _PROPOSAL)
        before_output = _make_promptfoo_output(frozen_total=2, frozen_passed=2, target_total=2, target_passed=2)
        after_output = _make_promptfoo_output(frozen_total=2, frozen_passed=2, target_total=2, target_passed=2)
        result = self._run_gate_with_fake_promptfoo(tmp_path, before_output, after_output)
        assert result.passed is True

    def test_gate_fails_when_frozen_regression_exceeds_tolerance(self, tmp_path: Path):
        self._patch_apply_diff(tmp_path, _PROPOSAL)
        # Before: 4/4 frozen pass. After: 2/4 frozen pass → 50% drop > 5% tolerance
        before_output = _make_promptfoo_output(frozen_total=4, frozen_passed=4, target_total=2, target_passed=2)
        after_output = _make_promptfoo_output(frozen_total=4, frozen_passed=2, target_total=2, target_passed=2)
        result = self._run_gate_with_fake_promptfoo(tmp_path, before_output, after_output)
        assert result.passed is False
        assert "frozen" in result.reason.lower()

    def test_gate_fails_when_target_pass_rate_below_min(self, tmp_path: Path):
        self._patch_apply_diff(tmp_path, _PROPOSAL)
        # Target: 0/2 pass = 0% < 50% required
        before_output = _make_promptfoo_output(frozen_total=2, frozen_passed=2, target_total=2, target_passed=0)
        after_output = _make_promptfoo_output(frozen_total=2, frozen_passed=2, target_total=2, target_passed=0)
        result = self._run_gate_with_fake_promptfoo(tmp_path, before_output, after_output)
        assert result.passed is False
        assert "target" in result.reason.lower()

    def test_gate_vacuously_passes_with_no_test_cases(self, tmp_path: Path):
        self._patch_apply_diff(tmp_path, _PROPOSAL)
        empty_output = _make_promptfoo_output(
            frozen_total=0, frozen_passed=0, target_total=0, target_passed=0
        )
        result = self._run_gate_with_fake_promptfoo(tmp_path, empty_output, empty_output)
        assert result.passed is True

    def test_gate_raises_when_promptfoo_absent(self, tmp_path: Path):
        with patch("silphco.selfimprove.gate._promptfoo_available", return_value=False):
            with pytest.raises(GatePreconditionError, match="promptfoo not found"):
                run_gate(_PROPOSAL, repo_root=tmp_path)

    def test_gate_raises_on_eval_error_with_empty_output(self, tmp_path: Path):
        self._patch_apply_diff(tmp_path, _PROPOSAL)
        with patch("silphco.selfimprove.gate._promptfoo_available", return_value=True), \
             patch("silphco.selfimprove.gate._run_promptfoo", return_value=("", "error msg", 1)):
            with pytest.raises(GateEvalError):
                run_gate(_PROPOSAL, repo_root=tmp_path)

    def test_gate_result_carries_stats(self, tmp_path: Path):
        self._patch_apply_diff(tmp_path, _PROPOSAL)
        before_output = _make_promptfoo_output(frozen_total=3, frozen_passed=3, target_total=2, target_passed=2)
        after_output = _make_promptfoo_output(frozen_total=3, frozen_passed=3, target_total=2, target_passed=2)
        result = self._run_gate_with_fake_promptfoo(tmp_path, before_output, after_output)
        assert result.frozen_before.total == 3
        assert result.frozen_before.passed == 3
        assert result.frozen_after.total == 3
        assert result.target_after.total == 2

    def test_gate_config_override_changes_tolerance(self, tmp_path: Path):
        self._patch_apply_diff(tmp_path, _PROPOSAL)
        # 2/4 frozen pass after = 50% drop; with tolerance=0.6 it should pass
        before_output = _make_promptfoo_output(frozen_total=4, frozen_passed=4, target_total=2, target_passed=2)
        after_output = _make_promptfoo_output(frozen_total=4, frozen_passed=2, target_total=2, target_passed=2)
        result = self._run_gate_with_fake_promptfoo(
            tmp_path, before_output, after_output,
            config={"frozen_tolerance": 0.6, "target_min_pass_rate": 0.0},
        )
        assert result.passed is True
