"""Config-driven persona routing.

When a task carries no explicit persona, PersonaRouter selects one from a
routing table keyed on goal text and/or changed-file scope.  An explicit
task persona always wins; the router is only consulted on the fallback path.

Config shape (in fleet.yaml or .agent-fleet.yaml under ``persona_routing``):

    persona_routing:
      rules:
        - goal_pattern: "(?i)front.?end|css|react"
          persona: frontend
        - scope_prefix: "packages/data"
          persona: data-eng
        - goal_pattern: "(?i)docs?"
          scope_prefix: "docs/"
          persona: tech-writer
      default_persona: coder   # optional — overrides config.default_persona

A rule matches when ALL specified matchers match:
- ``goal_pattern`` — Python regex searched against the goal string.
- ``scope_prefix`` — the scope string starts with this prefix.

Rules are evaluated in order; the first match wins.  When no rule matches the
router falls back to ``rules.default_persona``, then ``config.default_persona``,
then the hard-coded sentinel ``"coder"``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RoutingRule:
    persona: str
    goal_pattern: str | None = None  # compiled at route() time; None means skip
    scope_prefix: str | None = None  # None means skip


@dataclass
class PersonaRoutingConfig:
    rules: list[RoutingRule] = field(default_factory=list)
    default_persona: str | None = None  # None → defer to FleetConfig.default_persona


def parse_persona_routing(raw: dict[str, Any] | None) -> PersonaRoutingConfig | None:
    """Parse the ``persona_routing`` block from fleet or repo config YAML."""
    if not raw:
        return None
    rules_raw = raw.get("rules") or []
    rules: list[RoutingRule] = []
    for entry in rules_raw:
        if not isinstance(entry, dict):
            continue
        persona = str(entry.get("persona") or "").strip()
        if not persona:
            continue
        rules.append(
            RoutingRule(
                persona=persona,
                goal_pattern=str(entry["goal_pattern"]) if entry.get("goal_pattern") else None,
                scope_prefix=str(entry["scope_prefix"]) if entry.get("scope_prefix") else None,
            )
        )
    default_persona_raw = raw.get("default_persona")
    return PersonaRoutingConfig(
        rules=rules,
        default_persona=str(default_persona_raw) if default_persona_raw else None,
    )


class PersonaRouter:
    """Selects a persona for a task using a config-driven routing table."""

    def __init__(self, routing: PersonaRoutingConfig, fallback: str = "coder") -> None:
        self._routing = routing
        self._fallback = fallback

    def route(self, goal: str, scope: str = "") -> str:
        """Return the best persona for *goal* and optional *scope*.

        Evaluates rules in declaration order; first match wins.  When no rule
        matches, returns ``routing.default_persona`` or the constructor fallback.
        """
        for rule in self._routing.rules:
            goal_ok = rule.goal_pattern is None or bool(re.search(rule.goal_pattern, goal))
            scope_ok = rule.scope_prefix is None or scope.startswith(rule.scope_prefix)
            if goal_ok and scope_ok:
                return rule.persona
        return self._routing.default_persona or self._fallback
