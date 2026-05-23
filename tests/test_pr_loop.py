"""Tests for PR loop review parsing and config."""

from __future__ import annotations

from pathlib import Path

from agent_fleet.pr_loop.config import load_pr_loop_config
from agent_fleet.pr_loop.review_parse import (
    find_reviewer_comment,
    has_blocking_findings,
    parse_review_risk,
)
from agent_fleet.repo import load_repo_config


SAMPLE_REVIEW = """\
## 🤖 Composer PR Analysis

**Risk Level:** 🟡 MEDIUM
<details open>
<summary>🟡 <b>MEDIUM</b> (2)</summary>
| # | Area | Finding |
|---|------|---------|
| 1 | ⚙️ backend | Missing test coverage |
</details>
"""


def test_has_blocking_findings_medium() -> None:
    assert has_blocking_findings(SAMPLE_REVIEW) is True


def test_has_blocking_findings_low() -> None:
    body = "**Risk Level:** 🟢 LOW\n<details><summary><b>LOW</b> (1)</summary>"
    assert has_blocking_findings(body) is False


def test_parse_review_risk_skips_agent_comments() -> None:
    comments = [
        {"body": "🤖 Agent: noop"},
        {"body": SAMPLE_REVIEW},
    ]
    assert parse_review_risk(comments) == "MEDIUM"


def test_find_reviewer_comment() -> None:
    comments = [{"body": SAMPLE_REVIEW}]
    assert find_reviewer_comment(comments, marker="Composer PR Analysis") == SAMPLE_REVIEW


def test_lake_of_rage_pr_loop_config_loads() -> None:
    repo = load_repo_config("/home/evan/Documents/lake-of-rage/.agent-fleet.yaml")
    assert repo.pr_loop is not None
    assert repo.pr_loop.enabled is True
    assert repo.pr_loop.auto_merge is True


def test_load_pr_loop_defaults() -> None:
    cfg = load_pr_loop_config(Path("/tmp"), {"pr_loop": {"enabled": True}})
    assert cfg is not None
    assert cfg.branch_prefixes == ("fleet/",)
    assert cfg.max_fix_attempts == 2
