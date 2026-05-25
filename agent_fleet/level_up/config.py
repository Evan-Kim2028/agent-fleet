"""Level-up configuration parsed from repo YAML."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LevelUpConfig:
    train: bool = True
    contribute_to_fleet: bool = True
    journal_task_summaries: bool = True

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> LevelUpConfig:
        if not raw:
            return cls()
        return cls(
            train=bool(raw.get("train", True)),
            contribute_to_fleet=bool(raw.get("contribute_to_fleet", True)),
            journal_task_summaries=bool(raw.get("journal_task_summaries", True)),
        )


def load_level_up_config(raw: dict[str, Any] | None) -> LevelUpConfig | None:
    section = (raw or {}).get("level_up")
    if section is None:
        return None
    if not isinstance(section, dict):
        return LevelUpConfig()
    return LevelUpConfig.from_dict(section)
