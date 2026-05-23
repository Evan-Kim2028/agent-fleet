"""ImplementationBrief contract: dataclass + JSON schema validation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import jsonschema

from agent_fleet._schema import load_schema


@dataclass(frozen=True)
class ImplementationBrief:
    issue_number: int
    summary: str
    files_to_create: list[str]
    files_to_modify: list[str]
    test_strategy: str
    acceptance_criteria: list[str]
    references: list[dict[str, str]]
    rollback_plan: str | None = field(default=None)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImplementationBrief:
        validate_implementation_brief(data)
        return cls(
            issue_number=data["issue_number"],
            summary=data["summary"],
            files_to_create=list(data["files_to_create"]),
            files_to_modify=list(data["files_to_modify"]),
            test_strategy=data["test_strategy"],
            acceptance_criteria=list(data["acceptance_criteria"]),
            references=list(data["references"]),
            rollback_plan=data.get("rollback_plan"),
        )


def validate_implementation_brief(data: dict[str, Any]) -> None:
    """Raise jsonschema.ValidationError if data does not match implementation_brief schema."""
    jsonschema.validate(instance=data, schema=load_schema("implementation_brief"))
