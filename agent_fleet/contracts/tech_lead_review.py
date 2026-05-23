"""TechLeadReview contract: dataclass + JSON schema validation."""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass
from typing import Any

import jsonschema

from agent_fleet._schema import load_schema


class TechLeadVerdict(str, enum.Enum):
    APPROVE = "approve"
    BLOCK = "block"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class TechLeadReview:
    pr_number: int
    verdict: TechLeadVerdict
    summary: str
    escalation_required: bool
    disagreement_with_planner: str | None
    cross_pr_concerns: list[str]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["verdict"] = self.verdict.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TechLeadReview:
        validate_tech_lead_review(data)
        return cls(
            pr_number=data["pr_number"],
            verdict=TechLeadVerdict(data["verdict"]),
            summary=data["summary"],
            escalation_required=data["escalation_required"],
            disagreement_with_planner=data["disagreement_with_planner"],
            cross_pr_concerns=list(data["cross_pr_concerns"]),
        )


def validate_tech_lead_review(data: dict[str, Any]) -> None:
    """Raise jsonschema.ValidationError if data does not match tech_lead_review schema."""
    jsonschema.validate(instance=data, schema=load_schema("tech_lead_review"))
