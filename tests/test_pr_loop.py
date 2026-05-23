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
    comments: list[dict[str, object]] = [
        {"body": "🤖 Agent: noop"},
        {"body": SAMPLE_REVIEW},
    ]
    assert parse_review_risk(comments) == "MEDIUM"


def test_find_reviewer_comment() -> None:
    comments: list[dict[str, object]] = [{"body": SAMPLE_REVIEW}]
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
        "src/tests/test_agent_fleet_smoke.py",
    ]
    assert _files_outside_pr_scope(pr_files, [".agent-fleet.yaml"]) == ()
    assert _files_outside_pr_scope(
        pr_files, ["src/tests/test_new.py"]
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


def test_pr_loop_config_loads() -> None:
    raw = {
        "name": "sample-app",
        "pr_loop": {"enabled": True, "auto_merge": True, "fix_persona": "coder"},
    }
    cfg = load_pr_loop_config(Path("/tmp"), raw)
    assert cfg is not None
    assert cfg.enabled is True
    assert cfg.auto_merge is True


def test_prioritize_fleet_prs_newest_ready_first() -> None:
    from agent_fleet.pr_loop.watcher import prioritize_fleet_prs

    prs = [
        {"number": 1601, "labels": [], "isDraft": False},
        {"number": 1625, "labels": [{"name": "fleet-ready"}], "isDraft": False},
        {"number": 1624, "labels": [{"name": "fleet-ready"}], "isDraft": True},
    ]
    state = {"pr:1601": {"parked": True}}
    ordered = prioritize_fleet_prs(prs, state, fleet_ready_label="fleet-ready")
    assert [int(p["number"]) for p in ordered] == [1625, 1624, 1601]


def test_worktree_candidates_legacy_kimi() -> None:
    from agent_fleet.pr_loop.worktree import worktree_candidates

    base = Path("/tmp/agent-worktrees")
    cands = worktree_candidates("fleet/data/1532-0837d5d0", base)
    assert cands[0] == base / "1532-data-0837d5d0"
    assert base / "0837d5d0" in cands
    assert base / "fleet_data_1532-0837d5d0" in cands


def test_persona_from_branch_agent_prefix() -> None:
    from agent_fleet.pr_loop.lifecycle import persona_from_branch

    assert persona_from_branch("agent/backend/1499-abc12345", "backend") == "backend"
    assert persona_from_branch("fleet/data/1532-0837d5d0", "backend") == "data"


def test_persona_covering_files_and_merge_scope() -> None:
    from agent_fleet.pr_loop.lifecycle import (
        _merge_scope_out_of_scope,
        _persona_covering_files,
    )
    from agent_fleet.repo import RepoConfig

    repo = RepoConfig(
        repo_root=Path("/tmp"),
        persona_scope_allowlist={
            "coder": ("src/",),
            "infra": ("infra/", "sql/"),
            "pipe": ("pipelines/",),
        },
    )
    infra_files = ["infra/vps/deploy.sh", "infra/vps/rollback.sh"]
    assert _persona_covering_files(infra_files, repo) == "infra"
    assert _merge_scope_out_of_scope("coder", infra_files, repo) == []
    assert _merge_scope_out_of_scope("coder", ["src/a.py"], repo) == []
    assert _merge_scope_out_of_scope("coder", ["web/x.ts"], repo) == ["web/x.ts"]
    mixed = ["infra/vps/deploy.sh", "pipelines/pokemontcg_pipe/src/pipe/promote.py"]
    assert _merge_scope_out_of_scope("lakestore", mixed, repo) == []
