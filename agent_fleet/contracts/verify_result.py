"""VerifyResult contract: dataclass + JSON schema validation."""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass
from typing import Any

import jsonschema

from agent_fleet._schema import load_schema


class VerifySeverity(str, enum.Enum):
    OK = "ok"
    RETRY = "retry"
    FATAL = "fatal"


@dataclass(frozen=True)
class VerifyResult:
    severity: VerifySeverity
    checks: list[dict[str, Any]]
    violating_paths: list[str]
    files_changed: list[str]
    message: str

    @property
    def passed(self) -> bool:
        return self.severity is VerifySeverity.OK

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VerifyResult:
        validate_verify_result(data)
        return cls(
            severity=VerifySeverity(data["severity"]),
            checks=list(data["checks"]),
            violating_paths=list(data["violating_paths"]),
            files_changed=list(data["files_changed"]),
            message=data["message"],
        )


def validate_verify_result(data: dict[str, Any]) -> None:
    """Raise jsonschema.ValidationError if data does not match verify_result schema."""
    jsonschema.validate(instance=data, schema=load_schema("verify_result"))
