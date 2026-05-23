"""ResearchNote contract: dataclass + JSON schema validation."""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass
from typing import Any

import jsonschema

from agent_fleet._schema import load_schema


class Confidence(enum.StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class ResearchNote:
    research_id: str
    question: str
    findings: str
    scope_paths: list[str]
    referenced_files: list[str]
    confidence: Confidence

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["confidence"] = self.confidence.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResearchNote:
        validate_research_note(data)
        return cls(
            research_id=data["research_id"],
            question=data["question"],
            findings=data["findings"],
            scope_paths=list(data["scope_paths"]),
            referenced_files=list(data["referenced_files"]),
            confidence=Confidence(data["confidence"]),
        )


def validate_research_note(data: dict[str, Any]) -> None:
    """Raise jsonschema.ValidationError if data does not match research_note schema."""
    jsonschema.validate(instance=data, schema=load_schema("research_note"))
