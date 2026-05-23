"""Fleet ReviewResult contract: dataclass + JSON schema validation."""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass
from typing import Any

import jsonschema

from agent_fleet._schema import load_schema


class ReviewVerdict(str, enum.Enum):
    APPROVE = "approve"
    BLOCK = "block"
    REQUEST_CHANGES = "request_changes"


@dataclass(frozen=True)
class ReviewResult:
    pr_number: int
    verdict: ReviewVerdict
    summary: str
    issues: list[dict[str, str | None]]
    shard_id: str | None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["verdict"] = self.verdict.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReviewResult:
        validate_review(data)
        return cls(
            pr_number=data["pr_number"],
            verdict=ReviewVerdict(data["verdict"]),
            summary=data["summary"],
            issues=list(data["issues"]),
            shard_id=data["shard_id"],
        )


def validate_review(data: dict[str, Any]) -> None:
    """Raise jsonschema.ValidationError if data does not match review schema."""
    jsonschema.validate(instance=data, schema=load_schema("review"))
