"""Fleet DesignReview contract: dataclass + JSON schema validation.

Mirrors ``fleet.contracts.review`` — same validation pattern, same schema
loader, same frozen dataclass shape.

The ``scores`` dict is intentionally open-ended (``dict[str, int]``) so
dimension names come from the rubric/config, not from this module.  The JSON
schema constrains only that values are integers 0-100.

Verdict semantics:
  pass       — no blocking issues; ship as-is.
  needs_work — advisory issues present; implementer may address before merge.
  block      — hard gate: design issues must be fixed before PR can be promoted.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

import jsonschema

from agent_fleet._schema import load_schema


class DesignVerdict(enum.StrEnum):
    PASS = "pass"
    NEEDS_WORK = "needs_work"
    BLOCK = "block"


@dataclass(frozen=True)
class DesignIssue:
    """One issue found by the design critic."""

    severity: str            # "low" | "medium" | "high"
    area: str                # design dimension / surface
    screenshot_ref: str | None  # CaptureArtifact.ref or None
    fix: str                 # actionable recommendation


@dataclass(frozen=True)
class DesignReview:
    """Result produced by the DESIGN_REVIEW phase.

    Attributes:
        scores:  dimension → score (0-100).  Dimensions are data-driven; the
                 schema does not enumerate them.
        issues:  list of DesignIssue records found by the critic.
        verdict: overall gate decision.
    """

    scores: dict[str, int]
    issues: list[DesignIssue]
    verdict: DesignVerdict

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DesignReview:
        """Construct from a raw dict, validating against the JSON schema first."""
        validate_design_review(data)
        issues = [
            DesignIssue(
                severity=item["severity"],
                area=item["area"],
                screenshot_ref=item.get("screenshot_ref"),
                fix=item["fix"],
            )
            for item in data["issues"]
        ]
        return cls(
            scores=dict(data["scores"]),
            issues=issues,
            verdict=DesignVerdict(data["verdict"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scores": dict(self.scores),
            "issues": [
                {
                    "severity": iss.severity,
                    "area": iss.area,
                    "screenshot_ref": iss.screenshot_ref,
                    "fix": iss.fix,
                }
                for iss in self.issues
            ],
            "verdict": self.verdict.value,
        }

    @classmethod
    def neutral_pass(cls, dimensions: tuple[str, ...] = ()) -> DesignReview:
        """Return a schema-valid neutral pass result (no executor call needed).

        Used when there are no capture artifacts to review — the design review
        phase skips the executor and returns this instead of blocking.

        Args:
            dimensions: optional tuple of dimension names to populate with a
                neutral score of 100.  When empty, ``scores`` is ``{}``.
        """
        return cls(
            scores=dict.fromkeys(dimensions, 100),
            issues=[],
            verdict=DesignVerdict.PASS,
        )


def validate_design_review(data: dict[str, Any]) -> None:
    """Raise jsonschema.ValidationError if data does not match design_review schema."""
    jsonschema.validate(instance=data, schema=load_schema("design_review"))
