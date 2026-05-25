"""Tests for in-flight dispatch reaping."""

from __future__ import annotations

from unittest.mock import patch

from agent_fleet.in_flight import reap_in_flight


def test_reap_in_flight_drops_dead_pids() -> None:
    state = {
        "in_flight": {
            "100": [{"pid": 99999, "persona": "backend", "visual_audit": False}],
            "101": [],
        }
    }
    with patch("agent_fleet.in_flight.pid_is_dispatch", return_value=False):
        reaped = reap_in_flight(state)
    assert reaped == 1
    assert "100" not in state["in_flight"]
    assert "101" not in state["in_flight"]


def test_reap_in_flight_keeps_alive_pids() -> None:
    state = {
        "in_flight": {
            "100": [{"pid": 42, "persona": "backend", "visual_audit": False}],
        }
    }
    with patch("agent_fleet.in_flight.pid_is_dispatch", return_value=True):
        reaped = reap_in_flight(state)
    assert reaped == 0
    assert state["in_flight"]["100"][0]["pid"] == 42
