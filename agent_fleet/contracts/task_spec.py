"""TaskSpec contract: dataclass + JSON schema validation.

The TaskSpec is the structured output of the Planner phase. It declares
how an issue should be handled (single agent vs. decomposed), the scope
of allowed file changes, a research plan, acceptance criteria, and risk tier.
"""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass
from typing import Any

import jsonschema

from agent_fleet._schema import load_schema


class DecompositionDecision(str, enum.Enum):
    SINGLE = "single"
    DECOMPOSE = "decompose"
    REJECTED = "rejected"


class RiskTier(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class Scope:
    allowed_paths: list[str]
    forbidden_paths: list[str]


@dataclass(frozen=True)
class TaskSpec:
    issue_number: int
    decomposition_decision: DecompositionDecision
    decomposition_reason: str
    child_issues_proposed: list[dict[str, str]]
    scope: Scope
    research_plan: list[dict[str, Any]]
    acceptance_criteria: list[str]
    risk_tier: RiskTier
    critical_paths_touched: list[str]
    coordination_spec: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["decomposition_decision"] = self.decomposition_decision.value
        d["risk_tier"] = self.risk_tier.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskSpec:
        validate_task_spec(data)
        return cls(
            issue_number=data["issue_number"],
            decomposition_decision=DecompositionDecision(data["decomposition_decision"]),
            decomposition_reason=data["decomposition_reason"],
            child_issues_proposed=list(data["child_issues_proposed"]),
            scope=Scope(
                allowed_paths=list(data["scope"]["allowed_paths"]),
                forbidden_paths=list(data["scope"]["forbidden_paths"]),
            ),
            research_plan=list(data["research_plan"]),
            acceptance_criteria=list(data["acceptance_criteria"]),
            risk_tier=RiskTier(data["risk_tier"]),
            critical_paths_touched=list(data["critical_paths_touched"]),
            coordination_spec=data["coordination_spec"],
        )


def validate_task_spec(data: dict[str, Any]) -> None:
    """Raise jsonschema.ValidationError if data does not match task_spec schema."""
    jsonschema.validate(instance=data, schema=load_schema("task_spec"))
