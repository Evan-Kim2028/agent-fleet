"""Stall detection: one automatic continue, then escalate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_fleet.fleet_ops.stall import (
    ACTION_CONTINUE,
    ACTION_ESCALATE,
    ACTION_NONE,
    CONTINUE_PROMPT,
    judge_stall,
    should_escalate_after_continue,
    stream_idle_seconds,
)

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class _FakeStat:
    st_mtime: float


def test_fresh_activity_is_not_a_stall() -> None:
    verdict = judge_stall(idle_s=60.0, stall_minutes=20)
    assert verdict.action == ACTION_NONE
    assert verdict.stalled is False


def test_exactly_at_threshold_counts_as_a_stall() -> None:
    """The comparison is `<`, so idle == threshold is already a stall."""
    verdict = judge_stall(idle_s=20 * 60, stall_minutes=20)
    assert verdict.action == ACTION_CONTINUE


def test_past_threshold_asks_for_one_continue() -> None:
    verdict = judge_stall(idle_s=21 * 60, stall_minutes=20)
    assert verdict.action == ACTION_CONTINUE
    assert verdict.stalled is True
    assert "no tool activity" in verdict.reason


def test_second_stall_escalates() -> None:
    verdict = judge_stall(idle_s=30 * 60, stall_minutes=20, continues_used=1)
    assert verdict.action == ACTION_ESCALATE
    assert "did not resume" in verdict.reason


def test_dead_process_is_not_reported_as_stalled() -> None:
    """A finished run is not a stall; continuing a dead pid would be pointless."""
    verdict = judge_stall(idle_s=999.0, stall_minutes=1, process_alive=False)
    assert verdict.action == ACTION_NONE
    assert "not running" in verdict.reason


def test_zero_stall_minutes_treats_any_idle_as_stall() -> None:
    assert judge_stall(idle_s=0.5, stall_minutes=0).action == ACTION_CONTINUE


def test_negative_idle_is_not_a_stall() -> None:
    """A negative idle (clock skew) is 'active', not quiet."""
    assert judge_stall(idle_s=-5.0, stall_minutes=0).action == ACTION_NONE


def test_max_continues_is_configurable() -> None:
    assert judge_stall(idle_s=60.0, stall_minutes=0, continues_used=1, max_continues=2).action == (
        ACTION_CONTINUE
    )


def test_stream_idle_seconds_from_a_real_file(tmp_path: Path) -> None:
    path = tmp_path / "impl.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    now = path.stat().st_mtime + 120
    assert stream_idle_seconds(path, now=now) == 120.0


def test_stream_idle_seconds_with_injected_stat(tmp_path: Path) -> None:
    path = tmp_path / "impl.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    assert stream_idle_seconds(path, now=1000.0, stat=_FakeStat(940.0)) == 60.0


def test_stream_idle_seconds_missing_file_is_infinite() -> None:
    assert stream_idle_seconds("/definitely/not/here.jsonl") == float("inf")


def test_continue_prompt_tells_the_agent_what_to_do() -> None:
    """The prompt is the contract with the agent; keep it actionable."""
    lowered = CONTINUE_PROMPT.lower()
    assert "commit" in lowered
    assert "push" in lowered
    assert "pr" in lowered
    assert "do not re-explore" in lowered


def test_escalate_only_after_a_continue_was_spent() -> None:
    assert should_escalate_after_continue(stalled_again=True, continues_used=1) is True
    assert should_escalate_after_continue(stalled_again=True, continues_used=0) is False
    assert should_escalate_after_continue(stalled_again=False, continues_used=1) is False


def test_verdict_serialises() -> None:
    payload = judge_stall(idle_s=1.0, stall_minutes=20).to_dict()
    assert payload["action"] == ACTION_NONE
    assert payload["idle_s"] == 1.0


def test_idle_is_measured_in_seconds() -> None:
    """Guard against a units bug: idle_s is seconds, threshold is minutes*60.

    30 minutes of quiet is 1800 seconds and must read as a stall under a
    20-minute threshold; if judge_stall treated the input as minutes the same
    1800 would be a false non-stall.
    """
    assert judge_stall(idle_s=1800.0, stall_minutes=20).action == ACTION_CONTINUE
    assert judge_stall(idle_s=600.0, stall_minutes=20).action == ACTION_NONE
