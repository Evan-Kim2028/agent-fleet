"""Engine invocation: the policy pin, the fences, the ladders, the resume."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import pytest

from agent_fleet.fleet_ops.engines import (
    CONTINUE_PROMPT,
    DEFAULT_MAX_TURNS,
    RESUME_EXIT_CODE,
    build_prompt,
    looks_like_capacity_error,
    looks_truncated,
    read_task_file,
    run_cmd_engine,
    run_devin_engine,
)
from agent_fleet.fleet_ops.models import ModelPolicyError

STREAM = "\n".join(
    [
        json.dumps({"type": "tool_completed", "subtype": "completed"}),
        json.dumps({"type": "result", "finalText": "done"}),
    ]
)

if TYPE_CHECKING:
    from pathlib import Path


def _recording_runner(responses, calls=None):  # noqa: ANN001, ANN202
    """A runner that replays *responses* in order and records the argv it saw."""

    def runner(args, **_kwargs: object):  # noqa: ANN001, ANN202
        if calls is not None:
            calls.append(list(args))
        rc, out, err = responses[min(len(calls or []) - 1, len(responses) - 1)]
        return subprocess.CompletedProcess(list(args), rc, out, err)

    return runner


# ------------------------------------------------------------------- defaults


def test_max_turns_default_is_900() -> None:
    # documents-1d's requirement; fbrun used 300 and lanes ran out of turns.
    assert DEFAULT_MAX_TURNS == 900


def test_resume_exit_code_is_eight() -> None:
    assert RESUME_EXIT_CODE == 8


# --------------------------------------------------------------------- prompt


def test_prompt_carries_the_handoff_note_and_the_task() -> None:
    prompt = build_prompt("Do the thing.", lane="movers", branch="fb/movers")
    assert "HANDOFF NOTE" in prompt
    assert "Do the thing." in prompt
    assert "fb/movers" in prompt


def test_prompt_carries_the_standing_fences() -> None:
    """Requirement 7: the fences ride along with every implementer prompt."""
    prompt = build_prompt("Do the thing.", extra_fences=("Repo rule: no vendor edits.",))
    assert "STANDING FENCES" in prompt
    assert "no vendor edits" in prompt
    # The house rules ship even when a repo adds its own.
    assert "no-verify" in prompt
    # Fences come before the task, so the task is read as operating under them.
    assert prompt.index("STANDING FENCES") < prompt.index("Do the thing.")


def test_continue_prompt_is_the_single_ported_one() -> None:
    assert "Continue the task from where you stopped" in CONTINUE_PROMPT
    assert "gh pr create" in CONTINUE_PROMPT


def test_read_task_file_reports_a_missing_file() -> None:
    with pytest.raises(FileNotFoundError, match="task file does not exist"):
        read_task_file("/nope/nothing.md")


def test_read_task_file_returns_the_text(tmp_path: Path) -> None:
    path = tmp_path / "task.md"
    path.write_text("# Task\nbody\n", encoding="utf-8")
    assert read_task_file(path) == "# Task\nbody\n"


# ------------------------------------------------------------ truncation cues


def test_truncation_markers_are_detected() -> None:
    assert looks_truncated("reached max output token limit")
    assert looks_truncated("response truncated")
    assert looks_truncated("max_output_tokens exceeded")
    assert not looks_truncated("all good")
    assert not looks_truncated("")


def test_capacity_error_is_detected() -> None:
    assert looks_like_capacity_error("we have capacity issues right now")
    assert looks_like_capacity_error("CAPACITY")
    assert not looks_like_capacity_error("rate limited")


# ----------------------------------------------------------------- cmd engine


def test_cmd_pins_the_policy_model(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    run_cmd_engine(
        workdir=tmp_path,
        prompt="p",
        run_dir=tmp_path / "runs",
        runner=_recording_runner([(0, STREAM, "")], calls),
    )
    assert "-m" in calls[0]
    assert calls[0][calls[0].index("-m") + 1] == "stealth/space-bunny-alpha"


def test_cmd_refuses_an_out_of_policy_model(tmp_path: Path) -> None:
    with pytest.raises(ModelPolicyError):
        run_cmd_engine(
            workdir=tmp_path,
            prompt="p",
            model="gpt-4",
            run_dir=tmp_path / "runs",
            runner=_recording_runner([(0, STREAM, "")]),
        )


def test_cmd_launches_memory_capped(tmp_path: Path) -> None:
    """Requirement 5: every agent runs under a memory cap."""
    calls: list[list[str]] = []
    run_cmd_engine(
        workdir=tmp_path,
        prompt="p",
        run_dir=tmp_path / "runs",
        use_systemd=True,
        runner=_recording_runner([(0, STREAM, "")], calls),
    )
    joined = " ".join(calls[0])
    assert "MemoryMax=" in joined
    assert "MemorySwapMax=0" in joined


def test_cmd_writes_the_stream_and_result_files(tmp_path: Path) -> None:
    result = run_cmd_engine(
        workdir=tmp_path,
        prompt="p",
        run_dir=tmp_path / "runs",
        use_systemd=False,
        runner=_recording_runner([(0, STREAM, "")]),
    )
    assert result.stream_path is not None
    assert result.stream_path.is_file()
    assert result.output_path is not None
    assert result.output_path.read_text() == "done"
    assert result.exit_code == 0


def test_cmd_judges_a_lazy_exit(tmp_path: Path) -> None:
    """Zero exit, zero tool calls: a silent failure the bash driver called 86."""
    no_tools = json.dumps({"type": "result", "finalText": "I would do X"})
    result = run_cmd_engine(
        workdir=tmp_path,
        prompt="p",
        run_dir=tmp_path / "runs",
        use_systemd=False,
        runner=_recording_runner([(0, no_tools, "")]),
    )
    assert result.lazy is True
    assert result.exit_code == 86


def test_cmd_resumes_once_on_exit_eight(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    runner = _recording_runner([(RESUME_EXIT_CODE, STREAM, ""), (0, STREAM, "")], calls)
    result = run_cmd_engine(
        workdir=tmp_path,
        prompt="p",
        run_dir=tmp_path / "runs",
        use_systemd=False,
        runner=runner,
    )
    assert result.resumes == 1
    assert result.exit_code == 0
    assert len(calls) == 2


def test_cmd_does_not_resume_when_a_pr_already_exists(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    runner = _recording_runner([(RESUME_EXIT_CODE, STREAM, "")], calls)
    result = run_cmd_engine(
        workdir=tmp_path,
        prompt="p",
        run_dir=tmp_path / "runs",
        use_systemd=False,
        runner=runner,
        pr_exists=lambda: True,
    )
    assert result.resumes == 0
    assert len(calls) == 1


def test_cmd_resumes_at_most_once(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    runner = _recording_runner([(RESUME_EXIT_CODE, STREAM, "")], calls)
    result = run_cmd_engine(
        workdir=tmp_path,
        prompt="p",
        run_dir=tmp_path / "runs",
        use_systemd=False,
        runner=runner,
        max_resumes=1,
    )
    # One initial attempt plus exactly one continue, never a loop.
    assert len(calls) == 2
    assert result.resumes == 1


# --------------------------------------------------------------- devin engine


def test_devin_refuses_a_foreign_model(tmp_path: Path) -> None:
    with pytest.raises(ModelPolicyError, match="ladder"):
        run_devin_engine(
            workdir=tmp_path,
            prompt="p",
            model="gpt-4",
            runner=_recording_runner([(0, "", "")]),
        )


def test_devin_walks_down_the_ladder_on_a_capacity_error(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    runner = _recording_runner([(1, "", "we have capacity issues"), (0, "finished", "")], calls)
    result = run_devin_engine(workdir=tmp_path, prompt="p", runner=runner, sleep=lambda _s: None)
    assert result.capacity_fallbacks == ["swe-2-medium"]
    assert result.exit_code == 0
    assert len(calls) == 2


def test_devin_does_not_walk_the_ladder_on_a_normal_failure(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    runner = _recording_runner([(1, "", "a real error")], calls)
    run_devin_engine(workdir=tmp_path, prompt="p", runner=runner, sleep=lambda _s: None)
    assert len(calls) == 1


def test_devin_auto_continues_once_when_truncated(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    runner = _recording_runner(
        [(0, "hit the max output token limit", ""), (0, "finished", "")], calls
    )
    result = run_devin_engine(workdir=tmp_path, prompt="p", runner=runner)
    assert result.truncated is True
    assert result.continued is True
    assert len(calls) == 2


def test_devin_skips_the_continue_when_a_pr_exists(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    runner = _recording_runner([(0, "max output token reached", "")], calls)
    result = run_devin_engine(workdir=tmp_path, prompt="p", runner=runner, pr_exists=lambda: True)
    assert result.continued is False
    assert len(calls) == 1
