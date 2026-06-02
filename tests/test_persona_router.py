"""Regression tests for PersonaRouter and its integration into _normalize_tasks.

Tests that FAIL without the PersonaRouter change and pass with it:
- A routing rule selects the expected persona.
- An explicit task persona overrides routing.
- Absent routing config preserves the current default ('coder').
"""

from __future__ import annotations

from pathlib import Path

from agent_fleet.config import load_fleet_config
from agent_fleet.dispatcher import _normalize_tasks
from agent_fleet.persona_router import (
    PersonaRouter,
    PersonaRoutingConfig,
    RoutingRule,
    parse_persona_routing,
)

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# PersonaRouter unit tests
# ---------------------------------------------------------------------------


def test_routing_rule_matches_goal_pattern() -> None:
    routing = PersonaRoutingConfig(
        rules=[RoutingRule(persona="frontend", goal_pattern="(?i)react|css")]
    )
    router = PersonaRouter(routing, fallback="coder")
    assert router.route("Fix the React button styles") == "frontend"


def test_routing_rule_no_match_returns_fallback() -> None:
    routing = PersonaRoutingConfig(
        rules=[RoutingRule(persona="frontend", goal_pattern="(?i)react|css")]
    )
    router = PersonaRouter(routing, fallback="coder")
    assert router.route("Add a new database migration") == "coder"


def test_routing_rule_default_persona_overrides_fallback() -> None:
    routing = PersonaRoutingConfig(
        rules=[RoutingRule(persona="frontend", goal_pattern="(?i)react")],
        default_persona="data-eng",
    )
    router = PersonaRouter(routing, fallback="coder")
    # goal doesn't match the rule → falls back to routing.default_persona
    assert router.route("Fix the SQL query") == "data-eng"


def test_routing_rules_first_match_wins() -> None:
    routing = PersonaRoutingConfig(
        rules=[
            RoutingRule(persona="frontend", goal_pattern="(?i)css"),
            RoutingRule(persona="data-eng", goal_pattern="(?i)css|sql"),
        ]
    )
    router = PersonaRouter(routing, fallback="coder")
    assert router.route("Update the CSS") == "frontend"


def test_scope_prefix_rule_matches() -> None:
    routing = PersonaRoutingConfig(
        rules=[RoutingRule(persona="data-eng", scope_prefix="packages/data")]
    )
    router = PersonaRouter(routing, fallback="coder")
    assert router.route("any goal", scope="packages/data/pipeline.py") == "data-eng"


def test_scope_prefix_rule_no_match() -> None:
    routing = PersonaRoutingConfig(
        rules=[RoutingRule(persona="data-eng", scope_prefix="packages/data")]
    )
    router = PersonaRouter(routing, fallback="coder")
    assert router.route("any goal", scope="packages/ui/button.tsx") == "coder"


def test_combined_rule_requires_both_matchers() -> None:
    routing = PersonaRoutingConfig(
        rules=[
            RoutingRule(
                persona="docs-writer",
                goal_pattern="(?i)docs?",
                scope_prefix="docs/",
            )
        ]
    )
    router = PersonaRouter(routing, fallback="coder")
    # goal matches but scope doesn't → no match
    assert router.route("Update docs", scope="src/main.py") == "coder"
    # both match → rule fires
    assert router.route("Update docs", scope="docs/guide.md") == "docs-writer"


# ---------------------------------------------------------------------------
# parse_persona_routing
# ---------------------------------------------------------------------------


def test_parse_persona_routing_none() -> None:
    assert parse_persona_routing(None) is None


def test_parse_persona_routing_empty_dict() -> None:
    assert parse_persona_routing({}) is None


def test_parse_persona_routing_full() -> None:
    raw = {
        "rules": [
            {"goal_pattern": "(?i)react", "persona": "frontend"},
            {"scope_prefix": "data/", "persona": "data-eng"},
        ],
        "default_persona": "researcher",
    }
    cfg = parse_persona_routing(raw)
    assert cfg is not None
    assert len(cfg.rules) == 2
    assert cfg.rules[0].persona == "frontend"
    assert cfg.rules[1].scope_prefix == "data/"
    assert cfg.default_persona == "researcher"


# ---------------------------------------------------------------------------
# _normalize_tasks integration
# ---------------------------------------------------------------------------


def test_normalize_routing_selects_persona() -> None:
    routing = PersonaRoutingConfig(
        rules=[RoutingRule(persona="frontend", goal_pattern="(?i)react|css")]
    )
    tasks, _ = _normalize_tasks(
        goal="Fix the React button",
        context="",
        persona=None,
        workspace=None,
        pipeline=None,
        tasks=None,
        routing=routing,
        default_persona="coder",
    )
    assert tasks[0].persona == "frontend"


def test_normalize_explicit_persona_overrides_routing() -> None:
    routing = PersonaRoutingConfig(
        rules=[RoutingRule(persona="frontend", goal_pattern="(?i)react|css")]
    )
    tasks, _ = _normalize_tasks(
        goal="Fix the React button",
        context="",
        persona="reviewer",  # explicit caller persona wins
        workspace=None,
        pipeline=None,
        tasks=None,
        routing=routing,
        default_persona="coder",
    )
    assert tasks[0].persona == "reviewer"


def test_normalize_task_level_persona_overrides_routing() -> None:
    routing = PersonaRoutingConfig(
        rules=[RoutingRule(persona="frontend", goal_pattern="(?i)react")]
    )
    tasks, _ = _normalize_tasks(
        goal=None,
        context=None,
        persona=None,
        workspace=None,
        pipeline=None,
        tasks=[{"goal": "Fix the React button", "persona": "specialist"}],
        routing=routing,
        default_persona="coder",
    )
    assert tasks[0].persona == "specialist"


def test_normalize_absent_routing_preserves_default() -> None:
    """When no routing config is present behavior is unchanged (falls back to default_persona)."""
    tasks, _ = _normalize_tasks(
        goal="Do something",
        context="",
        persona=None,
        workspace=None,
        pipeline=None,
        tasks=None,
        routing=None,
        default_persona="coder",
    )
    assert tasks[0].persona == "coder"


def test_normalize_absent_routing_no_persona_uses_coder() -> None:
    """Mirrors the pre-router behavior: no routing, no persona → 'coder'."""
    tasks, _ = _normalize_tasks(
        goal="Fix a bug",
        context="",
        persona=None,
        workspace=None,
        pipeline=None,
        tasks=None,
    )
    assert tasks[0].persona == "coder"


def test_normalize_scope_prefix_routing_threaded_through_batch() -> None:
    """scope_prefix rule must fire when a batch task entry carries a scope field."""
    routing = PersonaRoutingConfig(
        rules=[RoutingRule(persona="data-eng", scope_prefix="packages/data")]
    )
    tasks, _ = _normalize_tasks(
        goal=None,
        context=None,
        persona=None,
        workspace=None,
        pipeline=None,
        tasks=[{"goal": "Fix the pipeline", "scope": "packages/data/etl.py"}],
        routing=routing,
        default_persona="coder",
    )
    assert tasks[0].persona == "data-eng"


def test_normalize_scope_prefix_routing_no_match_uses_fallback() -> None:
    """scope_prefix rule must NOT fire when the task scope does not match."""
    routing = PersonaRoutingConfig(
        rules=[RoutingRule(persona="data-eng", scope_prefix="packages/data")]
    )
    tasks, _ = _normalize_tasks(
        goal=None,
        context=None,
        persona=None,
        workspace=None,
        pipeline=None,
        tasks=[{"goal": "Fix the UI button", "scope": "packages/ui/button.tsx"}],
        routing=routing,
        default_persona="coder",
    )
    assert tasks[0].persona == "coder"


# ---------------------------------------------------------------------------
# Load from config
# ---------------------------------------------------------------------------


def test_fleet_config_persona_routing_none_by_default() -> None:
    """fleet.example.yaml has no persona_routing — field should be None."""
    config = load_fleet_config(ROOT / "fleet.example.yaml")
    assert config.persona_routing is None


def test_fleet_config_persona_routing_parsed(tmp_path: Path) -> None:
    yaml_text = """\
default_persona: coder
persona_routing:
  rules:
    - goal_pattern: "(?i)frontend"
      persona: frontend
  default_persona: researcher
"""
    cfg_file = tmp_path / "fleet.yaml"
    cfg_file.write_text(yaml_text)
    config = load_fleet_config(cfg_file)
    assert config.persona_routing is not None
    assert len(config.persona_routing.rules) == 1
    assert config.persona_routing.default_persona == "researcher"
