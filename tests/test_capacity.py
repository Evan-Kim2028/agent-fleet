"""Tests for unified fleet capacity admission."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from agent_fleet.capacity import (
    FleetCapacity,
    FleetCapacityGate,
    count_in_flight,
)
from agent_fleet.capacity.config import CapacityTier, PerIssueLimits

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _alive_dispatch_pids() -> Iterator[None]:
    with patch("agent_fleet.in_flight.pid_is_dispatch", return_value=True):
        yield


def _gate(**overrides: object) -> FleetCapacityGate:
    base = FleetCapacity(
        max_dispatches=4,
        visual_audit=CapacityTier(
            max_concurrent=2,
            ram_gb=6.0,
            min_free_ram_gb=8.0,
        ),
        per_issue=PerIssueLimits(default=3, visual_audit=1),
    )
    for key, value in overrides.items():
        object.__setattr__(base, key, value)
    return FleetCapacityGate(base)


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
    admission = _gate().try_admit(
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
    capacity = FleetCapacity(
        visual_audit=CapacityTier(max_concurrent=4, ram_gb=6.0, min_free_ram_gb=8.0),
    )
    admission = FleetCapacityGate(capacity).try_admit(
        state,
        issue_number=1821,
        persona="frontend",
        is_visual_audit=True,
        available_ram_gb=10.0,
    )
    assert not admission.allowed
    assert admission.reason == "visual_audit_ram_reserved"


def test_visual_audit_allowed_with_headroom() -> None:
    state: dict[str, object] = {"in_flight": {}}
    admission = _gate().try_admit(
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
    capacity = FleetCapacity(per_issue=PerIssueLimits(default=3, visual_audit=1))
    admission = FleetCapacityGate(capacity).try_admit(
        state,
        issue_number=1821,
        persona="backend",
        is_visual_audit=True,
        available_ram_gb=72.0,
    )
    assert not admission.allowed
    assert admission.reason == "issue_at_capacity"


def test_fleet_at_capacity() -> None:
    state = {
        "in_flight": {
            str(i): [{"pid": i, "persona": "backend", "visual_audit": False}] for i in range(4)
        }
    }
    admission = _gate().try_admit(
        state,
        issue_number=9999,
        persona="backend",
        is_visual_audit=False,
        available_ram_gb=72.0,
    )
    assert not admission.allowed
    assert admission.reason == "fleet_at_capacity"


def test_load_capacity_config_from_yaml_shape() -> None:
    from agent_fleet.capacity.config import load_capacity_config

    capacity = load_capacity_config(
        {
            "capacity": {
                "max_dispatches": 8,
                "tiers": {
                    "visual_audit": {
                        "max_concurrent": 4,
                        "ram_gb": 6,
                        "min_free_ram_gb": 10,
                    },
                },
                "per_issue": {"default": 2, "visual_audit": 1},
                "run": {"max_research_workers": 6},
            }
        }
    )
    assert capacity.max_dispatches == 8
    assert capacity.visual_audit.max_concurrent == 4
    assert capacity.per_issue.default == 2
    assert capacity.run.max_research_workers == 6


def test_is_visual_audit_dispatch() -> None:
    from agent_fleet.capacity import is_visual_audit_dispatch

    assert is_visual_audit_dispatch(issue_labels=["visual-audit"], title="x")
    assert is_visual_audit_dispatch(title="[Visual] foo")
    assert is_visual_audit_dispatch(title="x", body="Use Playwright MCP on prod")
    assert not is_visual_audit_dispatch(title="backend refactor")
