"""Gate pipeline contracts: findings, verify verdicts, judge rulings, outcome.

Mirrors the ``contracts/*`` pattern — frozen dataclass + a hand-written draft-07
JSON schema in ``agent_fleet/schemas/`` + a ``validate_*`` function. The gate
asks each lens reviewer, verifier, and the judge for JSON conforming to these
schemas, so a malformed answer is rejected at the boundary rather than
propagating into the blocker list.
"""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from typing import Any

import jsonschema

from agent_fleet._schema import load_schema

__all__ = [
    "Finding",
    "FindingsReport",
    "GateOutcome",
    "JudgeReport",
    "RecheckReport",
    "VerifyReport",
    "VerifyVerdict",
    "validate_findings",
    "validate_judge",
    "validate_recheck",
    "validate_verify",
]


class VerifyVerdict(enum.StrEnum):
    """What a verifier concluded about one claimed blocker."""

    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    UNTESTABLE = "UNTESTABLE"


@dataclass(frozen=True)
class Finding:
    """One claimed blocker, as reported by a lens reviewer.

    ``testable`` is the reviewer's own judgement that a local test can show the
    defect; the pipeline still decides, by trying to run one.
    """

    id: str
    file: str
    line: int
    claim: str
    repro: str
    testable: bool = True
    lens: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Finding:
        return cls(
            id=str(data.get("id", "")),
            file=str(data.get("file", "")),
            line=int(data.get("line") or 0),
            claim=str(data.get("claim", "")),
            repro=str(data.get("repro", "")),
            testable=bool(data.get("testable", True)),
            lens=str(data.get("lens", "")),
        )


@dataclass(frozen=True)
class FindingsReport:
    """One lens reviewer's structured answer."""

    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"findings": [f.to_dict() for f in self.findings]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FindingsReport:
        raw = data.get("findings")
        items = raw if isinstance(raw, list) else []
        return cls(findings=[Finding.from_dict(i) for i in items if isinstance(i, dict)])


def validate_findings(data: dict[str, Any]) -> None:
    """Raise jsonschema.ValidationError if data does not match the findings schema."""
    jsonschema.validate(instance=data, schema=load_schema("gate_findings"))


@dataclass(frozen=True)
class VerifyReport:
    """One verifier's structured answer about a single claim."""

    verdict: VerifyVerdict
    test_file: str | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["verdict"] = self.verdict.value
        if d.get("test_file") is None:
            d.pop("test_file")
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VerifyReport:
        return cls(
            verdict=VerifyVerdict(data.get("verdict", "REJECTED")),
            test_file=(str(data["test_file"]) if data.get("test_file") else None),
            reason=str(data.get("reason", "")),
        )


def validate_verify(data: dict[str, Any]) -> None:
    """Raise jsonschema.ValidationError if data does not match the verify schema."""
    jsonschema.validate(instance=data, schema=load_schema("gate_verify"))


@dataclass(frozen=True)
class JudgeReport:
    """The single judge call: rulings on untestable claims plus its own pass."""

    untestable_rulings: list[dict[str, Any]] = field(default_factory=list)
    new_blockers: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "untestable_rulings": list(self.untestable_rulings),
            "new_blockers": [f.to_dict() for f in self.new_blockers],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JudgeReport:
        rulings_raw = data.get("untestable_rulings")
        rulings = (
            [r for r in rulings_raw if isinstance(r, dict)] if isinstance(rulings_raw, list) else []
        )
        blockers_raw = data.get("new_blockers")
        blockers = (
            [Finding.from_dict(b) for b in blockers_raw if isinstance(b, dict)]
            if isinstance(blockers_raw, list)
            else []
        )
        return cls(untestable_rulings=rulings, new_blockers=blockers)

    @property
    def confirmed_untestable(self) -> list[dict[str, Any]]:
        """Rulings the judge marked as real merge blockers."""
        return [r for r in self.untestable_rulings if r.get("real") is True]


def validate_judge(data: dict[str, Any]) -> None:
    """Raise jsonschema.ValidationError if data does not match the judge schema."""
    jsonschema.validate(instance=data, schema=load_schema("gate_judge"))


@dataclass(frozen=True)
class RecheckReport:
    """The judge's recheck of untestable blockers after the fix round."""

    unresolved: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"unresolved": list(self.unresolved)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RecheckReport:
        raw = data.get("unresolved")
        items = [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
        return cls(unresolved=items)


def validate_recheck(data: dict[str, Any]) -> None:
    """Raise jsonschema.ValidationError if data does not match the recheck schema."""
    jsonschema.validate(instance=data, schema=load_schema("gate_recheck"))


class GateOutcome(enum.StrEnum):
    """Terminal states of a gate run."""

    APPROVED = "APPROVED"
    NEEDS_ESCALATION = "NEEDS_ESCALATION"


@dataclass(frozen=True)
class GateOutcomeRecord:
    """The gate's verdict plus the evidence behind it."""

    outcome: GateOutcome
    sha: str
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "sha": self.sha,
            "reasons": list(self.reasons),
        }
