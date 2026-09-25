"""Repo/PR binding — the check that stops a lane judging the wrong PR.

The incident this exists for: a stray ``REVIEW_REPO`` sent lake-of-rage PR #3544
to silphcoanalytics PR #3544, and four review lenses started on the wrong code.
The binding is derived from the worktree's own ``origin`` and the PR's
``headRefName``, so nothing inherited from the environment can redirect it.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Any

from agent_fleet.fleet_ops import binding as b
from agent_fleet.fleet_ops.binding import (
    REFUSED_HEAD_MISMATCH,
    REFUSED_NO_ORIGIN,
    REFUSED_NO_PR,
    REFUSED_SLUG_MISMATCH,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _completed(
    argv: list[str], code: int = 0, out: str = "", err: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, code, out, err)


def _fake_git_and_gh(
    *,
    remote: str | None = "git@github.com:Evan-Kim2028/lake-of-rage.git",
    pr: dict[str, Any] | None = None,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def runner(args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        argv = list(args)
        if argv[:2] == ["git", "remote"]:
            return _completed(
                argv, 0 if remote else 1, (remote or "") + "\n", "" if remote else "no origin"
            )
        if argv[:2] == ["gh", "pr"]:
            return _completed(argv, 0, json.dumps([pr] if pr else []), "")
        return _completed(argv, 0, "", "")

    return runner


# ------------------------------------------------------------- remote parsing


def test_parses_ssh_remote() -> None:
    assert (
        b.parse_remote_slug("git@github.com:Evan-Kim2028/lake-of-rage.git")
        == "Evan-Kim2028/lake-of-rage"
    )


def test_parses_https_remote_with_and_without_git_suffix() -> None:
    assert b.parse_remote_slug("https://github.com/o/r.git") == "o/r"
    assert b.parse_remote_slug("https://github.com/o/r") == "o/r"
    assert b.parse_remote_slug("https://github.com/o/r/") == "o/r"


def test_parses_ssh_url_form() -> None:
    assert b.parse_remote_slug("ssh://git@github.com/o/r.git") == "o/r"


def test_rejects_garbage_remotes() -> None:
    assert b.parse_remote_slug("") is None
    assert b.parse_remote_slug("   ") is None
    # A bare word with no owner segment is not a usable slug: guessing one would
    # be exactly the kind of silent misresolution this module exists to stop.
    assert b.parse_remote_slug("lake-of-rage") is None
    assert b.parse_remote_slug("https://github.com/onlyowner") is None


# ----------------------------------------------------------------- resolution


def test_resolves_a_matching_binding(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    runner = _fake_git_and_gh(pr={"number": 3544, "headRefName": "fb/lane", "headRefOid": "a" * 40})
    result = b.resolve(wt, branch="fb/lane", runner=runner)
    assert result.ok
    assert result.binding is not None
    assert result.binding.repo_slug == "Evan-Kim2028/lake-of-rage"
    assert result.binding.pr == 3544
    assert result.binding.head_sha == "a" * 40


def test_refuses_when_pr_head_is_a_different_branch(tmp_path: Path) -> None:
    """The exact shape of the incident: right repo, wrong branch's PR."""
    wt = tmp_path / "wt"
    wt.mkdir()
    runner = _fake_git_and_gh(
        pr={"number": 3544, "headRefName": "dq1d/other-lane", "headRefOid": "b" * 40}
    )
    result = b.resolve(wt, branch="fb/lane", runner=runner)
    assert not result.ok
    assert result.reason == REFUSED_HEAD_MISMATCH
    assert "dq1d/other-lane" in result.detail


def test_refuses_when_no_origin_remote(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    result = b.resolve(
        wt,
        branch="fb/lane",
        runner=_fake_git_and_gh(remote=None, pr={"number": 1, "headRefName": "fb/lane"}),
    )
    assert not result.ok
    assert result.reason == REFUSED_NO_ORIGIN


def test_refuses_when_expected_slug_does_not_match(tmp_path: Path) -> None:
    """A silph lane pointed at a lake worktree must refuse, not "correct" itself."""
    wt = tmp_path / "wt"
    wt.mkdir()
    runner = _fake_git_and_gh(pr={"number": 9, "headRefName": "fb/lane", "headRefOid": "c" * 40})
    result = b.resolve(
        wt,
        branch="fb/lane",
        expected_slug="Evan-Kim2028/silphcoanalytics",
        runner=runner,
    )
    assert not result.ok
    assert result.reason == REFUSED_SLUG_MISMATCH
    assert "silphcoanalytics" in result.detail


def test_refuses_when_no_pr_for_the_branch(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    result = b.resolve(wt, branch="fb/lane", runner=_fake_git_and_gh(pr=None))
    assert not result.ok
    assert result.reason == REFUSED_NO_PR


def test_refuses_when_the_worktree_is_missing(tmp_path: Path) -> None:
    result = b.resolve(tmp_path / "nope", branch="fb/lane", runner=_fake_git_and_gh())
    assert not result.ok


def test_slug_comparison_is_case_insensitive(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    runner = _fake_git_and_gh(pr={"number": 5, "headRefName": "fb/lane", "headRefOid": "d" * 40})
    result = b.resolve(
        wt,
        branch="fb/lane",
        expected_slug="evan-kim2028/lake-of-rage",
        runner=runner,
    )
    assert result.ok


# --------------------------------------------------------------- gate env vars


def test_gate_env_pins_every_repo_identifying_variable(tmp_path: Path) -> None:
    bound = b.LaneBinding(
        repo_slug="Evan-Kim2028/lake-of-rage",
        branch="fb/lane",
        pr=3544,
        head_ref="fb/lane",
        head_sha="e" * 40,
        worktree=tmp_path,
    )
    env = b.gate_env(bound)
    # A stale ambient REVIEW_REPO must be overwritten, not merely shadowed.
    assert env["REVIEW_REPO"] == "Evan-Kim2028/lake-of-rage"
    assert env["REPO"] == "Evan-Kim2028/lake-of-rage"
    assert env["REPO_SLUG"] == "Evan-Kim2028/lake-of-rage"
    assert env["PR"] == "3544"
    assert env["PR_NUMBER"] == "3544"
    assert env["HEAD_REF"] == "fb/lane"
    assert env["HEAD_SHA"] == "e" * 40
