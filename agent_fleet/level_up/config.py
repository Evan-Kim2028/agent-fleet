"""Level-up configuration parsed from repo YAML."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LevelUpConfig:
    train: bool = True
    contribute_to_fleet: bool = True
    journal_task_summaries: bool = True
    auto_learn: bool = False
    min_experience_rows: int = 20
    min_repos_for_fleet: int = 1
    learn_cooldown_seconds: int = 3600

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> LevelUpConfig:
        if not raw:
            return cls()
        return cls(
            train=bool(raw.get("train", True)),
            contribute_to_fleet=bool(raw.get("contribute_to_fleet", True)),
            journal_task_summaries=bool(raw.get("journal_task_summaries", True)),
            auto_learn=bool(raw.get("auto_learn", False)),
            min_experience_rows=int(raw.get("min_experience_rows", 20)),
            min_repos_for_fleet=int(raw.get("min_repos_for_fleet", 1)),
            learn_cooldown_seconds=int(raw.get("learn_cooldown_seconds", 3600)),
        )


def load_level_up_config(raw: dict[str, Any] | None) -> LevelUpConfig | None:
    section = (raw or {}).get("level_up")
    if section is None:
        return None
    if not isinstance(section, dict):
        return LevelUpConfig()
    return LevelUpConfig.from_dict(section)
