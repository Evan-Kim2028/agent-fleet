"""Compaction: retire idle or low-value overlay rules."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_fleet.level_up.journal import append_journal
from agent_fleet.level_up.models import LevelUpRule
from agent_fleet.level_up.overlay import load_overlay, save_overlay
from agent_fleet.level_up.paths import COMPACTION_IDLE_DAYS, persona_dir


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _load_rule_touch(directory: Path) -> dict[str, str]:
    meta_path = directory / "meta.json"
    if not meta_path.is_file():
        return {}
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    touches = raw.get("rule_touch") or {}
    if not isinstance(touches, dict):
        return {}
    return {str(k): str(v) for k, v in touches.items()}


def touch_overlay_rules(repo_key: str, persona: str, rule_ids: list[str]) -> None:
    """Record that overlay rules were active on a dispatch."""
    directory = persona_dir(repo_key, persona)
    meta_path = directory / "meta.json"
    meta: dict[str, Any] = {}
    if meta_path.is_file():
        try:
            loaded = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                meta = loaded
        except (OSError, json.JSONDecodeError):
            meta = {}
    touches = dict(meta.get("rule_touch") or {})
    now = datetime.now(tz=UTC).isoformat()
    for rule_id in rule_ids:
        touches[rule_id] = now
    meta["rule_touch"] = touches
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def compact_persona(repo_key: str, persona: str) -> list[str]:
    """Retire rules idle longer than COMPACTION_IDLE_DAYS. Returns retired rule ids."""
    overlay = load_overlay(repo_key, persona)
    if not overlay.rules:
        return []

    directory = persona_dir(repo_key, persona)
    retired_dir = directory / "retired"
    retired_dir.mkdir(parents=True, exist_ok=True)
    touches = _load_rule_touch(directory)
    cutoff = datetime.now(tz=UTC) - timedelta(days=COMPACTION_IDLE_DAYS)

    kept: list[LevelUpRule] = []
    retired_ids: list[str] = []

    for rule in overlay.rules:
        if rule.pinned:
            kept.append(rule)
            continue

        touch_ts = _parse_ts(touches.get(rule.id))
        if touch_ts is None and rule.provenance:
            touch_ts = _parse_ts(str(rule.provenance[0].get("ts") or ""))

        if touch_ts is None:
            kept.append(rule)
            continue

        if touch_ts >= cutoff:
            kept.append(rule)
            continue

        retired_ids.append(rule.id)
        retired_path = retired_dir / f"{rule.id}.yaml"
        retired_path.write_text(
            json.dumps(
                {
                    "id": rule.id,
                    "kind": rule.kind,
                    "text": rule.text,
                    "reason": "idle_compaction",
                    "retired_at": datetime.now(tz=UTC).isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        append_journal(
            "level_up.compact.retired",
            repo_key,
            persona,
            data={"rule_id": rule.id, "reason": "idle_7d"},
        )

    if retired_ids:
        save_overlay(repo_key, persona, kept, generation=overlay.generation)

    return retired_ids
