"""Tests for PR loop review parsing and config."""

from __future__ import annotations

from pathlib import Path

from agent_fleet.pr_loop.config import load_pr_loop_config
from agent_fleet.pr_loop.review_parse import (
    find_reviewer_comment,
    has_blocking_findings,
    parse_review_risk,
)


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


def test_load_pr_loop_defaults() -> None:
    cfg = load_pr_loop_config(Path("/tmp"), {"pr_loop": {"enabled": True}})
    assert cfg is not None
    assert cfg.branch_prefixes == ("fleet/",)
    assert cfg.max_fix_attempts == 2
    assert cfg.ci_fix_persona is None
    assert cfg.poll_interval_s == 10
    assert cfg.review_poll_s == 10
    assert cfg.ci_poll_s == 10
    assert cfg.ci_register_poll_s == 5
    assert cfg.post_fix_poll_s == 15


def test_load_pr_loop_ci_fix_persona() -> None:
    cfg = load_pr_loop_config(
        Path("/tmp"),
        {"pr_loop": {"enabled": True, "fix_persona": "coder", "ci_fix_persona": "ci"}},
    )
    assert cfg is not None
    assert cfg.fix_persona == "coder"
    assert cfg.ci_fix_persona == "ci"


def test_files_outside_pr_scope() -> None:
    from agent_fleet.pr_loop.lifecycle import _files_outside_pr_scope

    pr_files = [
        ".github/workflows/pr-analyzer.yml",
        ".agent-fleet.yaml",
        "packages/lakestore/tests/test_agent_fleet_smoke.py",
    ]
    assert _files_outside_pr_scope(pr_files, [".agent-fleet.yaml"]) == ()
    assert _files_outside_pr_scope(
        pr_files, ["packages/lakestore/tests/test_new.py"]
    ) == ()
    assert _files_outside_pr_scope(pr_files, ["README.md"]) == ("README.md",)


def test_tiered_merge_allowed_with_and_without_risk() -> None:
    from agent_fleet.pr_loop.lifecycle import tiered_merge_allowed

    blocked, reason = tiered_merge_allowed(
        ci_green=True, risk="MEDIUM", out_of_scope=[], parked=False
    )
    assert blocked is False
    assert "MEDIUM" in reason

    allowed, reason = tiered_merge_allowed(
        ci_green=True, risk=None, out_of_scope=[], parked=False
    )
    assert allowed is True
    assert reason == ""


def test_lake_of_rage_pr_loop_config_loads() -> None:
    raw = {
        "name": "lake-of-rage",
        "pr_loop": {"enabled": True, "auto_merge": True, "fix_persona": "coder"},
    }
    cfg = load_pr_loop_config(Path("/tmp"), raw)
    assert cfg is not None
    assert cfg.enabled is True
    assert cfg.auto_merge is True
