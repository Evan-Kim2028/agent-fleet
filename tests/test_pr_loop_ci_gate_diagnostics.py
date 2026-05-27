"""Tests for pr_loop CI-gate diagnostic surfacing.

The original failure mode: the loop reported `ci_failed` for fleet PRs whose
only failing check was on the `ignored_ci_checks` list. There was no
diagnostic in the trace explaining which check the gate considered failed.
These tests pin the new behavior — `PrChecksSnapshot.ignored_failed` is
populated and `wait_for_ci_green` puts both lists in the LifecycleResult
detail.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from agent_fleet.pr_loop import github_ops
from agent_fleet.pr_loop.config import PrLoopConfig
from agent_fleet.pr_loop.lifecycle import wait_for_ci_green

if TYPE_CHECKING:
    from pathlib import Path


def _checks_payload(checks: list[dict[str, str]]) -> bytes:
    import json

    return json.dumps(checks).encode()


def test_pr_checks_separates_ignored_failed_from_real_failed() -> None:
    fake_stdout = _checks_payload(
        [
            {"name": "composer pr analysis", "state": "FAILURE", "bucket": "fail"},
            {"name": "frontend tests", "state": "SUCCESS", "bucket": "pass"},
            {"name": "real ci check", "state": "FAILURE", "bucket": "fail"},
        ]
    ).decode()

    class _Result:
        returncode = 0
        stdout = fake_stdout

    with patch("agent_fleet.pr_loop.github_ops._gh", return_value=_Result()):
        snap = github_ops.pr_checks(
            42,
            cwd=None,
            ignored=("composer pr analysis", "kimi pr analysis"),
        )

    assert [c["name"] for c in snap.failed] == ["real ci check"]
    assert [c["name"] for c in snap.ignored_failed] == ["composer pr analysis"]
    assert [c["name"] for c in snap.all_filtered] == ["frontend tests", "real ci check"]


def test_pr_checks_returns_empty_ignored_failed_when_only_real_failures() -> None:
    fake_stdout = _checks_payload(
        [
            {"name": "real ci check", "state": "FAILURE", "bucket": "fail"},
        ]
    ).decode()

    class _Result:
        returncode = 0
        stdout = fake_stdout

    with patch("agent_fleet.pr_loop.github_ops._gh", return_value=_Result()):
        snap = github_ops.pr_checks(42, cwd=None, ignored=("composer pr analysis",))

    assert [c["name"] for c in snap.failed] == ["real ci check"]
    assert snap.ignored_failed == []


def test_wait_for_ci_green_detail_lists_suppressed_fails(tmp_path: Path) -> None:
    """The previously-opaque ci_failed detail must now name both the gating
    failures AND any ignored-but-failing checks. This is the diagnostic that
    lets an operator see at a glance whether the filter is doing its job."""
    snap = github_ops.PrChecksSnapshot(
        all_filtered=[{"name": "real ci check", "bucket": "fail"}],
        pending=[],
        failed=[{"name": "real ci check", "bucket": "fail"}],
        ignored_failed=[{"name": "composer pr analysis", "bucket": "fail"}],
    )
    with patch("agent_fleet.pr_loop.lifecycle.github_ops.pr_checks", return_value=snap):
        result = wait_for_ci_green(
            42,
            repo_root=tmp_path,
            loop_config=PrLoopConfig(),
            timeout_s=1,
        )

    assert result.status == "ci_failed"
    assert "real ci check" in result.detail
    assert "suppressed-fails" in result.detail
    assert "composer pr analysis" in result.detail


def test_wait_for_ci_green_returns_green_when_only_ignored_check_failed(
    tmp_path: Path,
) -> None:
    """The whole point of the ignored filter: composer pr analysis failing
    must NOT block the merge gate. This pins that behavior so a future change
    can't regress it silently."""
    snap = github_ops.PrChecksSnapshot(
        all_filtered=[{"name": "frontend tests", "bucket": "pass"}],
        pending=[],
        failed=[],
        ignored_failed=[{"name": "composer pr analysis", "bucket": "fail"}],
    )
    with patch("agent_fleet.pr_loop.lifecycle.github_ops.pr_checks", return_value=snap):
        result = wait_for_ci_green(
            42,
            repo_root=tmp_path,
            loop_config=PrLoopConfig(),
            timeout_s=1,
        )

    assert result.status == "ci_green"
