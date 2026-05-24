"""Tests for PR analyzer helpers."""

from __future__ import annotations

from agent_fleet.pr_review.analyzer import merge_analyses, passes_for_files
from agent_fleet.pr_review.config import PrReviewConfig
from agent_fleet.pr_review.git import is_deletion_only_pr, is_trivial_pr
from agent_fleet.pr_review.verdict import analysis_to_review_result, risk_to_verdict


def test_is_trivial_pr_docs_only() -> None:
    assert is_trivial_pr(["README.md", "docs/guide.md"], PrReviewConfig().trivial_patterns)


def test_is_deletion_only_pr() -> None:
    diff = "--- a/x.py\n+++ b/x.py\n-old\n-old2\n"
    assert is_deletion_only_pr(diff)
    assert not is_deletion_only_pr("--- a/x.py\n+++ b/x.py\n+new\n-old\n")


def test_passes_for_files_includes_frontend_when_present() -> None:
    config = PrReviewConfig()
    modes = passes_for_files(["frontend/app.tsx", "packages/foo.py"], config)
    assert "backend-security" in modes
    assert "frontend" in modes


def test_merge_analyses_keeps_highest_risk() -> None:
    merged = merge_analyses(
        [
            {"risk_level": "low", "primary_areas": [], "findings": [], "suggestions": []},
            {"risk_level": "high", "primary_areas": ["api"], "findings": [], "suggestions": []},
        ]
    )
    assert merged["risk_level"] == "high"


def test_risk_to_verdict_maps_critical_to_block() -> None:
    assert (
        risk_to_verdict("critical", [])
        == __import__(
            "agent_fleet.contracts.review", fromlist=["ReviewVerdict"]
        ).ReviewVerdict.BLOCK
    )


def test_analysis_to_review_result() -> None:
    review = analysis_to_review_result(
        {
            "risk_level": "medium",
            "summary": "Needs tests",
            "findings": [{"severity": "medium", "area": "tests", "message": "missing test"}],
        }
    )
    assert review.verdict.value == "request_changes"
    assert review.summary == "Needs tests"
