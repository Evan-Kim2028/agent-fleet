"""Invariants and characterization tests for autonomy.decide."""

from __future__ import annotations

from agent_fleet.autonomy import (
    Action,
    AutonomyEvidence,
    CiEvidence,
    Finding,
    PathEvidence,
    ReviewEvidence,
    decide,
)


def _green_ci(head: str = "aaa") -> CiEvidence:
    return CiEvidence(
        head_sha=head,
        required_checks={"tests": "pass"},
        all_non_ignored_green=True,
        pending=False,
        ready=True,
    )


def _red_ci(head: str = "aaa") -> CiEvidence:
    return CiEvidence(
        head_sha=head,
        required_checks={"tests": "fail"},
        all_non_ignored_green=False,
        pending=False,
        ready=True,
    )


def _low_review(head: str | None = "aaa") -> ReviewEvidence:
    return ReviewEvidence(head_sha=head, overall_risk="LOW", findings=())


def _medium_review(head: str | None = "aaa") -> ReviewEvidence:
    return ReviewEvidence(
        head_sha=head,
        overall_risk="MEDIUM",
        findings=(Finding(severity="MEDIUM", count=2),),
    )


def test_i1_critical_path_parks() -> None:
    """I1: critical path → PARK (before merge/fix)."""
    decision = decide(
        AutonomyEvidence(
            review=_low_review(),
            ci=_green_ci(),
            paths=PathEvidence(
                changed_files=("src/ok.py", ".github/workflows/ci.yml"),
                critical_prefixes=(".github/workflows/",),
            ),
            pr_head_sha="aaa",
        )
    )
    assert decision.action == Action.PARK
    assert decision.park_reason is not None
    assert "protected" in decision.reason.lower() or ".github" in decision.reason


def test_i2_medium_risk_without_address_not_merge() -> None:
    """I2: MEDIUM risk without address → not MERGE."""
    decision = decide(
        AutonomyEvidence(
            review=_medium_review(),
            ci=_green_ci(),
            paths=PathEvidence(changed_files=("src/a.py",), critical_prefixes=()),
            pr_head_sha="aaa",
            review_addressed_for_sha=None,
        )
    )
    assert decision.action != Action.MERGE
    assert decision.action == Action.FIX_REVIEW


def test_i3_addressed_for_sha_a_not_valid_for_sha_b() -> None:
    """I3: review_addressed for sha A, evidence for sha B with MEDIUM → FIX_REVIEW."""
    decision = decide(
        AutonomyEvidence(
            review=_medium_review(head="bbb"),
            ci=_green_ci(head="bbb"),
            paths=PathEvidence(changed_files=("src/a.py",), critical_prefixes=()),
            pr_head_sha="bbb",
            review_addressed_for_sha="aaa",  # addressed on older head only
        )
    )
    assert decision.action == Action.FIX_REVIEW


def test_i3_addressed_for_matching_sha_allows_merge() -> None:
    decision = decide(
        AutonomyEvidence(
            review=_medium_review(head="aaa"),
            ci=_green_ci(head="aaa"),
            paths=PathEvidence(changed_files=("src/a.py",), critical_prefixes=()),
            pr_head_sha="aaa",
            review_addressed_for_sha="aaa",
        )
    )
    assert decision.action == Action.MERGE


def test_i4_ci_not_green_not_merge() -> None:
    """I4: CI not green → not MERGE."""
    decision = decide(
        AutonomyEvidence(
            review=_low_review(),
            ci=_red_ci(),
            paths=PathEvidence(changed_files=("src/a.py",), critical_prefixes=()),
            pr_head_sha="aaa",
        )
    )
    assert decision.action != Action.MERGE
    assert decision.action == Action.FIX_CI


def test_low_risk_ci_green_no_critical_merges() -> None:
    decision = decide(
        AutonomyEvidence(
            review=_low_review(),
            ci=_green_ci(),
            paths=PathEvidence(changed_files=("src/a.py",), critical_prefixes=(".github/",)),
            pr_head_sha="aaa",
        )
    )
    assert decision.action == Action.MERGE


def test_wait_review_when_missing() -> None:
    decision = decide(
        AutonomyEvidence(
            review=None,
            ci=_green_ci(),
            pr_head_sha="aaa",
        )
    )
    assert decision.action == Action.WAIT_REVIEW


def test_ci_pending_noop() -> None:
    decision = decide(
        AutonomyEvidence(
            review=_low_review(),
            ci=CiEvidence(
                head_sha="aaa",
                all_non_ignored_green=False,
                pending=True,
                ready=False,
            ),
            pr_head_sha="aaa",
        )
    )
    assert decision.action == Action.NOOP
    assert "pending" in decision.reason.lower()


def test_security_medium_never_merges_even_if_addressed() -> None:
    review = ReviewEvidence(
        head_sha="aaa",
        overall_risk="MEDIUM",
        findings=(Finding(severity="MEDIUM", category="security", count=1),),
    )
    decision = decide(
        AutonomyEvidence(
            review=review,
            ci=_green_ci(),
            pr_head_sha="aaa",
            review_addressed_for_sha="aaa",
        )
    )
    assert decision.action != Action.MERGE
    assert decision.action == Action.PARK


def test_stale_review_head_invalidates_address() -> None:
    decision = decide(
        AutonomyEvidence(
            review=_medium_review(head="old"),
            ci=_green_ci(head="new"),
            pr_head_sha="new",
            review_addressed_for_sha="new",
        )
    )
    # review.head_sha != pr_head_sha → address invalidated → FIX_REVIEW
    assert decision.action == Action.FIX_REVIEW
