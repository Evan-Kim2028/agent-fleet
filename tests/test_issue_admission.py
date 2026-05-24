"""Tests for issue-loop dispatch admission."""

from __future__ import annotations

from agent_fleet.issue_loop.admission import check_dispatch_admission, count_in_flight
from agent_fleet.issue_loop.config import IssueDispatchConfig


def _config(**overrides: object) -> IssueDispatchConfig:
    base = IssueDispatchConfig(
        enabled=True,
        max_in_flight_per_issue=3,
        max_in_flight_visual_audit=1,
        max_concurrent_dispatches=4,
        max_concurrent_visual_audit=2,
        min_available_ram_gb=8.0,
        visual_audit_ram_gb=6.0,
    )
    for key, value in overrides.items():
        object.__setattr__(base, key, value)
    return base


def test_count_in_flight_visual_audit_only() -> None:
    state = {
        "in_flight": {
            "1821": [{"pid": 1, "persona": "frontend", "visual_audit": True}],
            "1822": [{"pid": 2, "persona": "backend", "visual_audit": False}],
        }
    }
    assert count_in_flight(state) == 2
    assert count_in_flight(state, visual_audit_only=True) == 1


def test_visual_audit_global_capacity() -> None:
    state = {
        "in_flight": {
            "1821": [{"pid": 1, "persona": "frontend", "visual_audit": True}],
            "1823": [{"pid": 2, "persona": "frontend", "visual_audit": True}],
        }
    }
    admission = check_dispatch_admission(
        _config(),
        state,
        issue_number=1824,
        persona="frontend",
        is_visual_audit=True,
        available_ram_gb=40.0,
    )
    assert not admission.allowed
    assert admission.reason == "visual_audit_at_capacity"


def test_visual_audit_ram_reservation() -> None:
    state = {
        "in_flight": {
            "1820": [{"pid": 1, "persona": "frontend", "visual_audit": True}],
        }
    }
    admission = check_dispatch_admission(
        _config(visual_audit_ram_gb=6.0),
        state,
        issue_number=1821,
        persona="frontend",
        is_visual_audit=True,
        available_ram_gb=10.0,
    )
    assert not admission.allowed
    assert admission.reason == "visual_audit_ram_reserved"


def test_visual_audit_allowed_with_headroom() -> None:
    state = {"in_flight": {}}
    admission = check_dispatch_admission(
        _config(),
        state,
        issue_number=1821,
        persona="frontend",
        is_visual_audit=True,
        available_ram_gb=72.0,
    )
    assert admission.allowed
    assert admission.reason == "ok"


def test_per_issue_visual_audit_limit() -> None:
    state = {
        "in_flight": {
            "1821": [{"pid": 1, "persona": "frontend", "visual_audit": True}],
        }
    }
    admission = check_dispatch_admission(
        _config(max_in_flight_visual_audit=1),
        state,
        issue_number=1821,
        persona="backend",
        is_visual_audit=True,
        available_ram_gb=72.0,
    )
    assert not admission.allowed
    assert admission.reason == "issue_at_capacity"
