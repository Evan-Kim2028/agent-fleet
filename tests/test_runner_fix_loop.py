"""v0.8.3 — verify-fix loop cost-optimization guards."""

from __future__ import annotations

from agent_fleet.fix_attempt import _truncate as _truncate_verify_message
from agent_fleet.fix_attempt import is_repeated_verify_failure
from agent_fleet.runner import FleetRunConfig


def test_default_max_verify_retries_is_one() -> None:
    """Each verify failure costs a full SYNTHESIZE+IMPLEMENT replay; cap at one
    retry so a non-converging fix triggers human triage instead of burning
    multi-million-token cache reads. Bumped from 3 → 1 in v0.8.3."""
    assert FleetRunConfig().max_verify_retries == 1


def test_truncate_verify_message_under_limit_passthrough() -> None:
    msg = "line 1\nline 2\nline 3"
    assert _truncate_verify_message(msg, max_lines=10) == msg


def test_truncate_verify_message_empty_passthrough() -> None:
    assert _truncate_verify_message("") == ""


def test_truncate_verify_message_clips_long_output() -> None:
    msg = "\n".join(f"line {i}" for i in range(200))
    out = _truncate_verify_message(msg, max_lines=50)
    lines = out.splitlines()
    assert lines[0] == "line 0"
    assert lines[49] == "line 49"
    assert lines[-1] == "... [150 more lines truncated]"


def test_is_repeated_verify_failure_empty_accumulated_is_false() -> None:
    assert is_repeated_verify_failure("boom", ()) is False


def test_is_repeated_verify_failure_empty_message_is_false() -> None:
    assert is_repeated_verify_failure("", ("boom",)) is False


def test_is_repeated_verify_failure_matches_last_entry() -> None:
    assert is_repeated_verify_failure("boom", ("boom",)) is True


def test_is_repeated_verify_failure_differs_from_last_entry() -> None:
    assert is_repeated_verify_failure("different", ("boom",)) is False


def test_is_repeated_verify_failure_earlier_nonlast_entry_is_false() -> None:
    assert is_repeated_verify_failure("A", ("A", "B")) is False
