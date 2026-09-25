"""Status-file lines and the per-operator approval/escalation hooks.

The line format is a contract with `automerge.sh` and both operators' tooling,
and the hook environment is a contract with documents-1d's shipper (``PR``,
``SHA9``) and its monitor (``exit=``). Both are tested here against the exact
spellings those consumers use.
"""

from __future__ import annotations

import re
import subprocess
from typing import TYPE_CHECKING

from agent_fleet.fleet_ops import statusfile as sf
from agent_fleet.fleet_ops.statusfile import (
    APPROVED_TOKEN,
    ESCALATION_TOKEN,
    append_status,
    approved_line,
    escalation_line,
    hook_env,
    last_status_line,
    read_status_lines,
    run_hook,
    short_sha,
)

if TYPE_CHECKING:
    from pathlib import Path

_HHMMSS = re.compile(r"^\d{2}:\d{2}:\d{2} ")

# ----------------------------------------------------------------- line format


def test_short_sha_is_nine_characters() -> None:
    assert short_sha("abcdef1234567890") == "abcdef123"
    assert short_sha("") == ""
    assert short_sha(None) == ""


def test_approved_line_shape() -> None:
    line = approved_line("abcdef1234567890")
    assert _HHMMSS.match(line), line
    assert f" {APPROVED_TOKEN} abcdef123" in line


def test_escalation_line_shape() -> None:
    line = escalation_line("commit_failed")
    assert _HHMMSS.match(line), line
    assert f" {ESCALATION_TOKEN} commit_failed" in line


def test_status_file_is_append_only(tmp_path: Path) -> None:
    """automerge tails this file, so an earlier line must survive a later write."""
    path = tmp_path / "lane.status"
    append_status(path, approved_line("aaa111bbb"))
    append_status(path, escalation_line("stalled"))
    lines = read_status_lines(path)
    assert len(lines) == 2
    assert APPROVED_TOKEN in lines[0]
    assert ESCALATION_TOKEN in lines[1]


def test_append_creates_missing_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "nested" / "lane.status"
    append_status(path, escalation_line("x"))
    assert path.is_file()


def test_reading_a_missing_status_file_is_empty(tmp_path: Path) -> None:
    assert read_status_lines(tmp_path / "nope.status") == []
    assert last_status_line(tmp_path / "nope.status") == ""


def test_last_status_line_filters_by_token(tmp_path: Path) -> None:
    path = tmp_path / "lane.status"
    append_status(path, approved_line("aaa111bbb"))
    append_status(path, "12:00:05 gate ran 4 lenses")
    # The verdict, not the chatter after it.
    assert APPROVED_TOKEN in last_status_line(path, tokens=(APPROVED_TOKEN, ESCALATION_TOKEN))
    assert last_status_line(path) == "12:00:05 gate ran 4 lenses"


def test_last_status_line_returns_empty_when_no_token_matches(tmp_path: Path) -> None:
    path = tmp_path / "lane.status"
    append_status(path, "12:00:05 gate ran")
    assert last_status_line(path, tokens=(APPROVED_TOKEN,)) == ""


# ---------------------------------------------------------------- hook env


def test_hook_env_carries_the_shipper_contract() -> None:
    env = hook_env(
        lane="movers",
        operator="documents-1d",
        repo="/home/evan/Documents/silphcoanalytics",
        pr=3544,
        sha="abcdef1234567890",
        status_file="/tmp/lane.status",
        verdict="approve",
    )
    # documents-1d writes dq/reviews/<PR>-<sha9>.md from exactly these two.
    assert env["PR"] == "3544"
    assert env["SHA9"] == "abcdef123"
    assert env["VERDICT"] == "approve"
    assert env["LANE"] == "movers"
    assert env["OPERATOR"] == "documents-1d"


def test_hook_env_exposes_exit_for_the_monitor() -> None:
    """documents-1d's monitor greps for `exit=<rc>` lines in the gate log."""
    env = hook_env(
        lane="l",
        operator="documents-1d",
        repo="/r",
        pr=1,
        sha="abc1234",
        status_file="/s",
        verdict="escalate",
        exit_code=86,
    )
    assert env["exit"] == "86"
    assert env["RC"] == "86"


def test_hook_env_omits_exit_when_there_is_none() -> None:
    env = hook_env(lane="l", operator="o", repo="/r", pr=None, sha=None, status_file="")
    assert "exit" not in env
    assert "RC" not in env
    assert env["PR"] == ""
    assert env["SHA9"] == ""


def test_hook_env_does_not_inherit_the_manager_environment() -> None:
    env = hook_env(lane="l", operator="o", repo="/r", pr=1, sha="abc1234", status_file="s")
    assert set(env) == {"LANE", "OPERATOR", "REPO", "PR", "SHA9", "STATUS", "VERDICT"}


# ------------------------------------------------------------------- running


def test_empty_command_does_not_run() -> None:
    def boom(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("must not run an empty hook")

    result = run_hook(None, {}, runner=boom)
    assert result.ran is False
    assert result.ok is True
    assert run_hook("   ", {}, runner=boom).ran is False


def test_successful_hook_reports_its_output() -> None:
    def runner(_cmd, **_kwargs: object):  # noqa: ANN001, ANN202
        return subprocess.CompletedProcess([], 0, "wrote dq/reviews/3544-abcdef123.md\n", "")

    result = run_hook("write-review", {"PR": "3544"}, runner=runner)
    assert result.ran is True
    assert result.ok is True
    assert "3544" in result.detail


def test_failing_hook_is_reported_but_not_raised() -> None:
    """A broken notifier must not rewrite an already-recorded lane verdict."""

    def runner(_cmd, **_kwargs: object):  # noqa: ANN001, ANN202
        return subprocess.CompletedProcess([], 7, "", "no such directory")

    result = run_hook("write-review", {}, runner=runner)
    assert result.ran is True
    assert result.ok is False
    assert result.exit_code == 7
    assert "no such directory" in result.detail


def test_a_real_hook_gets_the_documented_variables(tmp_path: Path) -> None:
    """End-to-end through a real shell, since that is how operators write hooks."""
    out = tmp_path / "env.txt"
    result = run_hook(
        f'printf \'%s|%s|%s\' "$PR" "$SHA9" "$exit" > {out}',
        hook_env(
            lane="l",
            operator="documents-1d",
            repo="/r",
            pr=3544,
            sha="abcdef1234567890",
            status_file="/s",
            verdict="escalate",
            exit_code=86,
        ),
        cwd=tmp_path,
    )
    assert result.ok, result.detail
    assert out.read_text() == "3544|abcdef123|86"


def test_hook_timeout_is_reported_not_raised() -> None:
    def runner(_cmd, **_kwargs: object):  # noqa: ANN001, ANN202
        raise subprocess.TimeoutExpired(cmd="x", timeout=1)

    result = run_hook("sleep 99", {}, timeout=1, runner=runner)
    assert result.ran is True
    assert result.ok is False
    assert "timed out" in result.detail


def test_hook_oserror_is_reported_not_raised() -> None:
    def runner(_cmd, **_kwargs: object):  # noqa: ANN001, ANN202
        raise OSError("no shell")

    result = run_hook("cmd", {}, runner=runner)
    assert result.ok is False
    assert "no shell" in result.detail


def test_tokens_are_the_documented_strings() -> None:
    assert sf.APPROVED_TOKEN == "PREMERGE-APPROVED"
    assert sf.ESCALATION_TOKEN == "NEEDS-ESCALATION"
