"""Load and compose persona level-up overlay rules."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from agent_fleet.level_up.models import LevelUpOverlay, LevelUpRule
from agent_fleet.level_up.paths import persona_dir


def load_overlay(repo_key: str, persona: str) -> LevelUpOverlay:
    """Load overlay rules and generation metadata for a repo persona."""
    directory = persona_dir(repo_key, persona)
    generation = _load_meta_generation(directory)

    overlay_path = directory / "overlay.yaml"
    if not overlay_path.is_file():
        return LevelUpOverlay(schema_version=1, rules=(), generation=generation)

    raw = yaml.safe_load(overlay_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raw = {}

    schema_version = int(raw.get("schema_version", 1))
    rules_raw = raw.get("rules") or []
    rules: list[LevelUpRule] = []
    if isinstance(rules_raw, list):
        for item in rules_raw:
            if isinstance(item, dict):
                rules.append(LevelUpRule.from_dict(item))

    return LevelUpOverlay(
        schema_version=schema_version,
        rules=tuple(rules),
        generation=generation,
    )


def _rule_text(rule: LevelUpRule | dict[str, Any]) -> str:
    if isinstance(rule, LevelUpRule):
        return rule.text.strip()
    return str(rule.get("text") or "").strip()


def compose_overlay_text(
    rules: tuple[LevelUpRule | dict[str, Any], ...] | list[LevelUpRule | dict[str, Any]],
) -> str:
    """Render overlay rules as bullet lines for persona compose."""
    lines: list[str] = []
    for rule in rules:
        text = _rule_text(rule)
        if text:
            lines.append(f"- {text}")
    return "\n".join(lines).strip()


def compose_overlay_prompt(
    rules: tuple[LevelUpRule, ...] | list[LevelUpRule],
    *,
    generation: int = 0,
) -> str:
    """Render overlay rules as a markdown prompt section."""
    if not rules:
        return ""

    lines = [f"# Level up (gen {generation})", ""]
    for rule in rules:
        lines.append(f"## {rule.id}")
        if rule.kind:
            lines.append(f"**Kind:** {rule.kind}")
        lines.append("")
        lines.append(rule.text.strip())
        lines.append("")

    return "\n".join(lines).rstrip()


def _load_meta(directory: Path) -> dict[str, Any]:
    meta_path = directory / "meta.json"
    if not meta_path.is_file():
        return {}
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _load_meta_generation(directory: Path) -> int:
    generation = _load_meta(directory).get("generation", 0)
    try:
        return int(generation)
    except (TypeError, ValueError):
        return 0


def _rule_to_dict(rule: LevelUpRule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "kind": rule.kind,
        "text": rule.text,
        "pinned": rule.pinned,
        "stack_tags": list(rule.stack_tags),
        "area_patterns": list(rule.area_patterns),
        "provenance": list(rule.provenance),
        "confidence": rule.confidence,
    }


def save_overlay(
    repo_key: str,
    persona: str,
    rules: list[LevelUpRule] | tuple[LevelUpRule, ...],
    *,
    generation: int | None = None,
) -> LevelUpOverlay:
    directory = persona_dir(repo_key, persona)
    directory.mkdir(parents=True, exist_ok=True)
    meta = _load_meta(directory)
    gen = generation if generation is not None else int(meta.get("generation", 0))
    payload = {
        "schema_version": 1,
        "rules": [_rule_to_dict(r) for r in rules],
    }
    (directory / "overlay.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )
    meta["generation"] = gen
    meta.setdefault("schema_version", 1)
    (directory / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return LevelUpOverlay(schema_version=1, rules=tuple(rules), generation=gen)


def write_candidate(
    repo_key: str,
    persona: str,
    candidate_id: str,
    rule: LevelUpRule,
    *,
    gate: dict[str, Any] | None = None,
) -> Path:
    directory = persona_dir(repo_key, persona) / "candidates"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{candidate_id}.json"
    path.write_text(
        json.dumps(
            {"rule": _rule_to_dict(rule), "gate": gate or {}},
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def load_candidate(
    repo_key: str,
    persona: str,
    candidate_id: str,
) -> tuple[LevelUpRule, dict[str, Any]]:
    path = persona_dir(repo_key, persona) / "candidates" / f"{candidate_id}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    rule = LevelUpRule.from_dict(raw.get("rule") or {})
    gate = raw.get("gate") if isinstance(raw.get("gate"), dict) else {}
    return rule, gate


def list_candidates(repo_key: str, persona: str) -> list[str]:
    directory = persona_dir(repo_key, persona) / "candidates"
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.json"))


def promote_rule(
    repo_key: str,
    persona: str,
    rule: LevelUpRule,
    *,
    bump_generation: bool = True,
) -> LevelUpOverlay:
    overlay = load_overlay(repo_key, persona)
    rules = [r for r in overlay.rules if r.id != rule.id]
    rules.append(rule)
    generation = overlay.generation + (1 if bump_generation else 0)
    return save_overlay(repo_key, persona, rules, generation=generation)
