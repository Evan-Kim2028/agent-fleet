"""Pure disposition logic: classify a completed run and decide what to do with it.

All functions here are side-effect-free; IO (push, open_pr) lives in the runner.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class DispositionKind(enum.StrEnum):
    OPEN_PR = "open_pr"
    SALVAGE = "salvage"
    ABANDON = "abandon"
    NOOP = "noop"


@dataclass(frozen=True)
class Disposition:
    kind: DispositionKind
    draft: bool
    outcome: str
    reason: str


@dataclass(frozen=True)
class RunFacts:
    verify_ok: bool
    verify_fatal: bool
    scope_violated: bool
    changed_files: tuple[str, ...]
    halted_by_controller: bool = False


@dataclass
class DispositionPolicy:
    salvage_on_verify_failed: bool = True
    salvage_on_scope_violation: bool = True
    # Extra labels appended to any salvage PR so triagers can filter easily.
    salvage_labels: list[str] = field(default_factory=lambda: ["fleet-salvage"])


def decide_disposition(facts: RunFacts, policy: DispositionPolicy) -> Disposition:
    """Map RunFacts + policy to a Disposition with no IO."""
    if facts.verify_ok:
        return Disposition(
            kind=DispositionKind.OPEN_PR,
            draft=False,
            outcome="completed",
            reason="verify passed",
        )

    if facts.verify_fatal:
        # Never salvage a fatal verify result; it may indicate verifier tampering.
        return Disposition(
            kind=DispositionKind.ABANDON,
            draft=False,
            outcome="error",
            reason="fatal verifier signal — will not salvage",
        )

    if facts.scope_violated:
        if policy.salvage_on_scope_violation and facts.changed_files:
            return Disposition(
                kind=DispositionKind.SALVAGE,
                draft=True,
                outcome="scope_violation_salvaged",
                reason="scope violation with changes — opening draft PR for human review",
            )
        return Disposition(
            kind=DispositionKind.ABANDON,
            draft=False,
            outcome="scope_violation",
            reason="scope violation — no changes to salvage" if not facts.changed_files
            else "scope violation — salvage disabled by policy",
        )

    if not facts.changed_files:
        return Disposition(
            kind=DispositionKind.NOOP,
            draft=False,
            outcome="completed_noop",
            reason="implementer produced no code changes",
        )

    if policy.salvage_on_verify_failed:
        return Disposition(
            kind=DispositionKind.SALVAGE,
            draft=True,
            outcome="verify_failed_salvaged",
            reason="verify failed with changes — opening draft PR for human review",
        )

    return Disposition(
        kind=DispositionKind.ABANDON,
        draft=False,
        outcome="verify_failed",
        reason="verify failed — salvage disabled by policy",
    )
