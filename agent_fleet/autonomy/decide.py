"""Pure autonomy policy: :class:`AutonomyEvidence` → :class:`Decision`."""

from __future__ import annotations

from agent_fleet.autonomy.parse_review import has_security_medium_plus, review_is_blocking
from agent_fleet.autonomy.types import (
    Action,
    AutonomyEvidence,
    Decision,
    PathEvidence,
    ReviewEvidence,
)


def critical_path_hits(paths: PathEvidence | None) -> list[str]:
    """Return changed files that match any critical path prefix."""
    if paths is None or not paths.critical_prefixes:
        return []
    hits: list[str] = []
    for path in paths.changed_files:
        if any(path.startswith(prefix) for prefix in paths.critical_prefixes):
            hits.append(path)
    return hits


def _review_addressed_for_current(evidence: AutonomyEvidence) -> bool:
    """True when review was marked addressed for the current PR head SHA."""
    pr_head = evidence.pr_head_sha
    addressed_sha = evidence.review_addressed_for_sha
    if not pr_head or not addressed_sha:
        return False
    return addressed_sha == pr_head


def _review_present(review: ReviewEvidence | None) -> bool:
    if review is None:
        return False
    return review.overall_risk is not None or bool(review.findings)


def decide(evidence: AutonomyEvidence) -> Decision:
    """Evaluate evidence and return the next PR-loop action.

    Evaluation order (Phases 1-4; see ADR 0002):

    1. **PARK** if any changed file matches ``critical_prefixes`` (I1).
    2. **WAIT_REVIEW** if no usable review evidence.
    3. SHA-keyed address: ``review_addressed_for_sha`` only clears blocking when
       it equals ``pr_head_sha`` (I3). A review ``head_sha`` mismatch with the
       PR head invalidates address.
    4. **FIX_REVIEW** if blocking (MEDIUM+ risk/counts) and not addressed (I2).
    5. Security category MEDIUM+ never admits **MERGE** (FIX_REVIEW or PARK).
    6. **FIX_CI** / **NOOP** when CI is not green (I4); pending → NOOP.
    7. **MERGE** when review is clear (or addressed for this SHA) and CI green.
    8. Else **NOOP**.
    """
    hits = critical_path_hits(evidence.paths)
    if hits:
        reason = f"Touches protected paths: {', '.join(hits[:5])}"
        return Decision(action=Action.PARK, reason=reason, park_reason=reason)

    review = evidence.review
    if not _review_present(review):
        return Decision(action=Action.WAIT_REVIEW, reason="No review evidence")

    assert review is not None  # for type checkers

    addressed = _review_addressed_for_current(evidence)
    # Stale review relative to PR head: do not treat as addressed for merge.
    if review.head_sha and evidence.pr_head_sha and review.head_sha != evidence.pr_head_sha:
        addressed = False

    security_block = has_security_medium_plus(review)
    if security_block:
        if not addressed:
            return Decision(
                action=Action.FIX_REVIEW,
                reason="Security findings MEDIUM+ require fix",
            )
        reason = "Security findings MEDIUM+ require human review"
        return Decision(action=Action.PARK, reason=reason, park_reason=reason)

    blocking = review_is_blocking(review, deletion_only=evidence.deletion_only)
    if blocking and not addressed:
        risk = review.overall_risk or "MEDIUM+"
        return Decision(
            action=Action.FIX_REVIEW,
            reason=f"Blocking review findings (risk={risk})",
        )

    ci = evidence.ci
    if ci is None:
        return Decision(action=Action.NOOP, reason="No CI evidence")

    if (ci.pending or not ci.ready) and not ci.all_non_ignored_green:
        return Decision(action=Action.NOOP, reason="CI pending")

    if not ci.all_non_ignored_green:
        return Decision(action=Action.FIX_CI, reason="CI not green")

    # Residual MEDIUM without address never reaches here (I2 / Phase 4).
    if blocking and not addressed:
        return Decision(
            action=Action.FIX_REVIEW,
            reason="Blocking review findings",
        )

    return Decision(
        action=Action.MERGE,
        reason="Review clear (or addressed for this SHA) and CI green",
    )
