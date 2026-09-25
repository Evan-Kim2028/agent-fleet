"""The gate seam — feature detection, and never judging the wrong PR."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from agent_fleet.fleet_ops import gate as g
from agent_fleet.fleet_ops.binding import LaneBinding


def _binding(**overrides: Any) -> LaneBinding:  # noqa: ANN401
    base: dict[str, Any] = {
        "repo_slug": "Evan-Kim2028/lake-of-rage",
        "branch": "fb/lane",
        "pr": 3544,
        "head_ref": "fb/lane",
        "head_sha": "abcdef1234567890",
        "worktree": Path("/tmp/wt"),
    }
    return LaneBinding(**{**base, **overrides})


# --------------------------------------------------------- feature detection


def test_known_subcommands_detect_the_gate() -> None:
    assert g.gate_available({"run", "gate", "lanes"})
    assert not g.gate_available({"run", "lanes"})
    assert g.gate_available("gate")
    assert not g.gate_available("lanes")


def test_detection_never_raises_when_the_binary_is_missing() -> None:
    # No monkeypatching: a missing binary must be a False, not an exception.
    assert isinstance(g.gate_available(), bool)


def test_detection_handles_a_failing_probe() -> None:
    def runner(_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        return subprocess.CompletedProcess([], 1, "", "boom")

    assert g.gate_available(runner=runner) is False


# ----------------------------------------------------------------- gate argv


def test_gate_argv_carries_the_verified_repo_and_head() -> None:
    args = g.build_gate_args(_binding(), lane="movers")
    assert args[1] == "gate"
    assert "--repo" in args
    assert "Evan-Kim2028/lake-of-rage" in args
    assert "--pr" in args
    assert "3544" in args
    assert "--head-ref" in args
    assert "fb/lane" in args


def test_judge_engine_is_only_added_when_set() -> None:
    assert "--judge-engine" not in g.build_gate_args(_binding(), lane="x")
    assert "cmd" in g.build_gate_args(_binding(), lane="x", judge_engine="cmd")


# --------------------------------------------------------------- the outcomes


def test_missing_gate_is_a_skip_not_an_error() -> None:
    """The parallel lane may not have merged; the lane must still succeed."""
    out = g.run_gate(lane="x", binding=_binding(), cwd=Path("/tmp"), known_subcommands={"run"})
    assert out.skipped
    assert not out.ran
    assert "not merged" in out.reason


def test_gate_approval_is_read_from_the_status_line() -> None:
    def runner(_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        return subprocess.CompletedProcess([], 0, "12:00:00 PREMERGE-APPROVED abc123def\n", "")

    out = g.run_gate(
        lane="x", binding=_binding(), cwd=Path("/tmp"), known_subcommands={"gate"}, runner=runner
    )
    assert out.available
    assert out.approved
    assert out.sha9 == "abc123def"


def test_a_bare_approve_word_is_not_an_approval() -> None:
    """Only the exact status contract approves; a stray "APPROVE" never reaches the merge path."""

    def runner(_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        return subprocess.CompletedProcess([], 0, "APPROVE\n", "")

    out = g.run_gate(
        lane="x", binding=_binding(), cwd=Path("/tmp"), known_subcommands={"gate"}, runner=runner
    )
    assert not out.approved
    assert out.sha9 is None


def test_an_approval_line_with_a_failing_exit_is_rejected() -> None:
    def runner(_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        return subprocess.CompletedProcess([], 5, "12:00:00 PREMERGE-APPROVED abcdef123\n", "")

    out = g.run_gate(
        lane="x", binding=_binding(), cwd=Path("/tmp"), known_subcommands={"gate"}, runner=runner
    )
    assert not out.approved


def test_stderr_logs_do_not_hide_the_stdout_status_line() -> None:
    def runner(_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        return subprocess.CompletedProcess(
            [], 0, "12:00:00 PREMERGE-APPROVED abcdef123\n", "INFO trailing log line\n"
        )

    out = g.run_gate(
        lane="x", binding=_binding(), cwd=Path("/tmp"), known_subcommands={"gate"}, runner=runner
    )
    assert out.approved
    assert out.sha9 == "abcdef123"


def test_needs_escalation_output_is_not_an_approval() -> None:
    """`NEEDS-ESCALATION ... APPROVE criteria not met` must not read as approved.

    The substring "APPROVE" is present, so an approval-first check would call
    this a pass. It is the most dangerous possible false positive here.
    """

    def runner(_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        return subprocess.CompletedProcess([], 1, "NEEDS-ESCALATION: did not APPROVE the fix\n", "")

    out = g.run_gate(
        lane="x", binding=_binding(), cwd=Path("/tmp"), known_subcommands={"gate"}, runner=runner
    )
    assert not out.approved


def test_nonzero_exit_without_approval_is_a_rejection() -> None:
    def runner(_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        return subprocess.CompletedProcess([], 3, "gate crashed\n", "")

    out = g.run_gate(
        lane="x", binding=_binding(), cwd=Path("/tmp"), known_subcommands={"gate"}, runner=runner
    )
    assert out.available
    assert not out.approved


def test_gate_prose_tail_is_not_an_approval() -> None:
    """`PREMERGE-APPROVED <prose>` is not the status contract: no approval, no sha9."""

    def runner(_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        return subprocess.CompletedProcess([], 0, "12:00:00 PREMERGE-APPROVED everything\n", "")

    out = g.run_gate(
        lane="x",
        binding=_binding(head_sha=""),
        cwd=Path("/tmp"),
        known_subcommands={"gate"},
        runner=runner,
    )
    assert not out.approved
    assert out.sha9 is None


def test_commit_env_overlays_skip_on_the_real_environment(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """A bare {"SKIP": ...} env stripped PATH/HOME and broke the hooks it meant to keep."""
    from agent_fleet.fleet_ops import guarantee

    seen: dict[str, Any] = {}

    def runner(args, **kwargs):  # noqa: ANN001, ANN003, ANN202
        seen.setdefault("env", kwargs.get("env"))
        return subprocess.CompletedProcess(args, 1, "", "stop here")

    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    guarantee.commit_worktree(
        tmp_path, engine="cmd", skip_hooks=("ruff-format",), runner=runner, lane="x"
    )
    env = seen["env"]
    assert isinstance(env, dict)
    assert env["SKIP"] == "ruff-format"
    assert env["PATH"] == "/usr/bin:/bin"
