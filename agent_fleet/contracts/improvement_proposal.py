from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ConfigChange:
    key: str
    current_value: Any
    proposed_value: Any
    reasoning: str


@dataclass
class PersonaChange:
    persona: str
    appended_note: str
    reasoning: str


@dataclass
class ImprovementProposal:
    config_changes: list[ConfigChange] = field(default_factory=list)
    persona_changes: list[PersonaChange] = field(default_factory=list)
    analysis_summary: str = ""
    data_window_runs: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> ImprovementProposal:
        return cls(
            config_changes=[ConfigChange(**c) for c in d.get("config_changes", [])],
            persona_changes=[PersonaChange(**p) for p in d.get("persona_changes", [])],
            analysis_summary=d.get("analysis_summary", ""),
            data_window_runs=d.get("data_window_runs", 0),
        )
