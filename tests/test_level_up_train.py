"""Tests for level-up train, gate, compaction, and tech-lead promotion."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_fleet.level_up import append_experience
from agent_fleet.level_up import journal as level_up_journal
from agent_fleet.level_up import paths as level_up_paths
from agent_fleet.level_up.compaction import compact_persona, touch_overlay_rules
from agent_fleet.level_up.gate import gate_rule
from agent_fleet.level_up.models import LevelUpRule
from agent_fleet.level_up.overlay import load_overlay, save_overlay, write_candidate
from agent_fleet.level_up.train import approve_candidate, find_overlay_overlap, train_persona
from agent_fleet.tech_lead import skill_promotion_review


@pytest.fixture
def level_up_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "level_up"
    root.mkdir(parents=True, exist_ok=True)
    index = tmp_path / "journal" / "index.jsonl"
    monkeypatch.setattr(level_up_paths, "LEVEL_UP_ROOT", root)
    monkeypatch.setattr(level_up_journal, "JOURNAL_INDEX_PATH", index)
    monkeypatch.setattr(level_up_paths, "JOURNAL_INDEX_PATH", index)
    return root


def test_gate_rejects_episodic_text() -> None:
    rule = LevelUpRule(id="x", kind="methodology", text="Fix issue #123 on branch feature/foo")
    result = gate_rule(rule, evidence_count=3, weighted_evidence=3.0)
    assert result.passed is False
    assert result.reject_reason == "episodic_pattern"


def test_gate_auto_promotes_methodology() -> None:
    rule = LevelUpRule(
        id="verify-before-done",
        kind="methodology",
        text="Run repo verify_commands before claiming completion.",
    )
    result = gate_rule(rule, evidence_count=1, weighted_evidence=2.0)
    assert result.passed is True
    assert result.needs_tech_lead is False


def test_gate_queues_domain_for_tech_lead() -> None:
    rule = LevelUpRule(
        id="iceberg-partition",
        kind="domain_data",
        text="When writing Iceberg tables, validate partition spec before publish.",
    )
    result = gate_rule(rule, evidence_count=2, weighted_evidence=2.0)
    assert result.passed is False
    assert result.needs_tech_lead is True


def test_train_promotes_from_verify_failed_experience(level_up_root: Path) -> None:  # noqa: ARG001
    append_experience(
        repo_key="demo-repo",
        persona="coder",
        source="dispatch",
        status="verify_failed",
        goal="Fix dispatcher tests",
        changed_files=["agent_fleet/dispatcher.py"],
    )

    result = train_persona("demo-repo", "coder", contribute_to_fleet=False)
    assert len(result.promoted) == 1

    overlay = load_overlay("demo-repo", "coder")
    assert len(overlay.rules) == 1
    assert "verify_commands" in overlay.rules[0].text


def test_train_queues_domain_candidate(level_up_root: Path) -> None:  # noqa: ARG001
    rule = LevelUpRule(
        id="iceberg-partition",
        kind="domain_data",
        text="When writing Iceberg tables, validate partition spec before publish.",
    )
    write_candidate(
        "demo-repo",
        "coder",
        rule.id,
        rule,
        gate={"kind": "domain_data", "needs_tech_lead": True},
    )

    verdict = approve_candidate("demo-repo", "coder", rule.id, contribute_to_fleet=False)
    assert verdict == "approve"
    overlay = load_overlay("demo-repo", "coder")
    assert any(r.id == rule.id for r in overlay.rules)


def test_skill_promotion_review_heuristic() -> None:
    rule = LevelUpRule(
        id="scope",
        kind="methodology",
        text="Keep changes scoped to the task goal; avoid unrelated edits.",
    )
    review = skill_promotion_review(rule, kind="methodology")
    assert review.verdict == "approve"


def test_compaction_retires_idle_rules(level_up_root: Path) -> None:  # noqa: ARG001
    rule = LevelUpRule(
        id="old-rule",
        kind="methodology",
        text="Run lint before pushing changes to remote branches.",
        provenance=(
            {
                "repo_key": "demo-repo",
                "ts": (datetime.now(tz=UTC) - timedelta(days=10)).isoformat(),
            },
        ),
    )
    save_overlay("demo-repo", "coder", [rule], generation=1)

    retired = compact_persona("demo-repo", "coder")
    assert retired == ["old-rule"]
    overlay = load_overlay("demo-repo", "coder")
    assert overlay.rules == ()


def test_touch_overlay_rules_updates_meta(level_up_root: Path) -> None:
    touch_overlay_rules("demo-repo", "coder", ["verify-before-done"])
    meta_path = level_up_root / "demo-repo" / "coder" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert "verify-before-done" in meta["rule_touch"]


def test_find_overlay_overlap(level_up_root: Path) -> None:  # noqa: ARG001
    shared = LevelUpRule(
        id="verify-before-done",
        kind="methodology",
        text="Run repo verify_commands before claiming completion.",
    )
    save_overlay("demo-repo", "coder", [shared], generation=1)
    save_overlay("_fleet", "coder", [shared], generation=1)

    overlaps = find_overlay_overlap("demo-repo", "coder")
    assert overlaps == [
        {
            "id": "verify-before-done",
            "repo_kind": "methodology",
            "fleet_kind": "methodology",
        }
    ]
