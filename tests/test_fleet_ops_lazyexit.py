"""Lazy-exit heuristic ported from the fbrun bash driver."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from agent_fleet.fleet_ops.lazyexit import (
    LAZY_EXIT_CODE,
    REFUSAL_TOOL_CALL_THRESHOLD,
    count_tool_calls,
    extract_final_text,
    judge_run,
    judge_stream_file,
    looks_like_refusal,
)

if TYPE_CHECKING:
    from pathlib import Path


def _stream(*records: dict) -> str:
    return "\n".join(json.dumps(r) for r in records) + "\n"


def _tool(n: int) -> list[dict]:
    return [{"type": "tool_completed", "subtype": "completed"} for _ in range(n)]


def test_no_tool_calls_is_a_lazy_exit() -> None:
    stream = _stream({"type": "result", "finalText": "I have completed the task."})
    verdict = judge_run(exit_code=0, stream_text=stream)
    assert verdict.lazy is True
    assert verdict.exit_code == LAZY_EXIT_CODE
    assert "no tool calls" in verdict.reason


def test_refusal_with_few_tool_calls_is_a_lazy_exit() -> None:
    stream = _stream(*_tool(3), {"type": "result", "finalText": "I cannot execute the tests here."})
    verdict = judge_run(exit_code=0, stream_text=stream)
    assert verdict.lazy is True
    assert verdict.exit_code == LAZY_EXIT_CODE
    assert verdict.tool_calls == 3


def test_healthy_run_with_many_tool_calls_is_not_lazy() -> None:
    stream = _stream(
        *_tool(REFUSAL_TOOL_CALL_THRESHOLD + 5),
        {"type": "result", "finalText": "I cannot execute the tests here."},
    )
    verdict = judge_run(exit_code=0, stream_text=stream)
    assert verdict.lazy is False
    assert verdict.exit_code == 0


def test_healthy_run_with_no_refusal_is_not_lazy() -> None:
    stream = _stream(*_tool(40), {"type": "result", "finalText": "PR opened as #3510."})
    assert judge_run(exit_code=0, stream_text=stream).lazy is False


def test_non_zero_exit_is_passed_through_unrejudged() -> None:
    """A real failure already signals itself; it is never relabelled a lazy exit."""
    verdict = judge_run(exit_code=3, stream_text="")
    assert verdict.lazy is False
    assert verdict.exit_code == 3


@pytest.mark.parametrize(
    "text",
    [
        "I can't run the tests in this environment.",
        "I cannot complete the task.",
        "I couldn't access the file.",
        "Unable to perform the migration.",
        "The tools are not available here.",
        "required shell tools",
    ],
)
def test_refusal_patterns_match(text: str) -> None:
    assert looks_like_refusal(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "PR opened as #3510.",
        "I ran the tests and they pass.",
        "The refactor is complete; see the diff.",
        "",
    ],
)
def test_non_refusals_do_not_match(text: str) -> None:
    assert looks_like_refusal(text) is False


def test_count_tool_calls_ignores_unparseable_lines() -> None:
    stream = "not json\n" + _stream(*_tool(2))
    assert count_tool_calls(stream) == 2


def test_count_tool_calls_recognises_both_spellings() -> None:
    """cmd emits tool_completed as either `type` or `subtype`."""
    stream = _stream(
        {"type": "tool_completed"},
        {"type": "assistant", "subtype": "tool_completed"},
        {"type": "assistant", "text": "working"},
    )
    assert count_tool_calls(stream) == 2


def test_extract_final_text_takes_the_last_result_record() -> None:
    stream = _stream(
        {"type": "result", "finalText": "first"},
        {"type": "result", "finalText": "second"},
    )
    assert extract_final_text(stream) == "second"


def test_extract_final_text_sentinel_when_absent() -> None:
    """fblane's cmd_ok test relied on this exact sentinel; keep the contract."""
    assert extract_final_text("") == "NO RESULT EVENT"
    assert extract_final_text('{"type": "assistant"}') == "NO RESULT EVENT"


def test_judge_run_uses_supplied_final_text() -> None:
    stream = _stream(*_tool(1))
    verdict = judge_run(exit_code=0, stream_text=stream, final_text="I cannot complete the task.")
    assert verdict.lazy is True


def test_judge_stream_file_reads_from_disk(tmp_path: Path) -> None:
    path = tmp_path / "impl.jsonl"
    path.write_text(_stream({"type": "result", "finalText": "done"}), encoding="utf-8")
    assert judge_stream_file(path, exit_code=0).lazy is True


def test_judge_stream_file_unreadable_is_lazy(tmp_path: Path) -> None:
    verdict = judge_stream_file(tmp_path / "nope.jsonl", exit_code=0)
    assert verdict.lazy is True
    assert "unreadable" in verdict.reason


def test_verdict_serialises() -> None:
    verdict = judge_run(exit_code=0, stream_text="")
    payload = verdict.to_dict()
    assert payload["lazy"] is True
    assert payload["exit_code"] == LAZY_EXIT_CODE
