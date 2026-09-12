"""Tests for PR analyzer helpers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from agent_fleet.pr_review.analyzer import merge_analyses, passes_for_files
from agent_fleet.pr_review.config import PrReviewConfig
from agent_fleet.pr_review.git import get_working_tree_diff, is_deletion_only_pr, is_trivial_pr
from agent_fleet.pr_review.verdict import analysis_to_review_result, risk_to_verdict
from agent_fleet.repo import RepoConfig

if TYPE_CHECKING:
    import pytest


def test_is_trivial_pr_docs_only() -> None:
    assert is_trivial_pr(["README.md", "docs/guide.md"], PrReviewConfig().trivial_patterns)


def test_is_trivial_pr_empty_file_list_is_not_trivial() -> None:
    # An empty changeset must never be vacuously classified as "trivial" —
    # that would auto-approve a run that changed nothing.
    assert not is_trivial_pr([], PrReviewConfig().trivial_patterns)


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


def test_working_tree_diff_includes_unstaged_and_untracked(tmp_path: Path) -> None:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "a.py").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
        cwd=repo,
        check=True,
    )
    (repo / "a.py").write_text("two\n", encoding="utf-8")
    (repo / "b.py").write_text("new\n", encoding="utf-8")
    diff, files = get_working_tree_diff(cwd=repo, base_branch="main")
    assert "a.py" in files
    assert "b.py" in files
    assert "two" in diff
    assert "new" in diff


def test_pr_analyzer_review_forwards_fleet_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_fleet.phases import run_pr_analyzer_review_phase

    captured: dict[str, object] = {}

    def fake_run_pr_review(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "analysis": {"summary": "ok", "findings": []},
            "review": {"verdict": "approve"},
            "verdict": "approve",
            "risk_level": "low",
            "comment_markdown": "",
        }

    monkeypatch.setattr("agent_fleet.phases.run_pr_review", fake_run_pr_review)
    fleet_config = MagicMock()
    repo = RepoConfig(
        repo_root=Path("/tmp"),
        pr_review=PrReviewConfig(enabled=True, use_in_code_review=True),
        default_branch="main",
    )
    run_pr_analyzer_review_phase(
        backend=MagicMock(),
        resolver=MagicMock(),
        task=MagicMock(),
        workspace=Path("/tmp"),
        timeout_s=10,
        changed_files=[],
        implementation_summary="done",
        repo=repo,
        fleet_config=fleet_config,
    )
    assert captured["fleet_config"] is fleet_config
