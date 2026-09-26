"""Tests for `fleet pr own`'s exit code.

A round that ends with a dead engine, a rejected push, or red tests is a failed
round. `emit` derives the exit code from the status/verdict/outcome tables, and
a round dict carries none of those keys, so it would return 0 for every one of
them — a gate or lane driver gating on the exit code would read a broken round
as progress and keep escalating.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_fleet.cli import main, round_succeeded

if TYPE_CHECKING:
    import pytest


def _round(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "pushed": True,
        "new_head": "abc123",
        "fixed": ["f-1"],
        "disputed": [],
        "tests": {"ran": True, "ok": True, "failing": []},
        "detail": "",
    }
    payload.update(overrides)
    return payload


def test_a_pushed_round_with_green_tests_succeeded() -> None:
    assert round_succeeded(_round()) is True


def test_an_engine_failure_is_not_a_successful_round() -> None:
    result = _round(pushed=False, tests={"ran": False, "ok": False}, detail="engine failed: boom")
    assert round_succeeded(result) is False


def test_a_failed_push_is_not_a_successful_round() -> None:
    result = _round(pushed=False, detail="push failed: ! [rejected] non-fast-forward")
    assert round_succeeded(result) is False


def test_red_tests_are_not_a_successful_round() -> None:
    result = _round(tests={"ran": True, "ok": False, "failing": ["tests/t.py::t1"]})
    assert round_succeeded(result) is False


def test_a_round_with_no_tests_configured_still_succeeds() -> None:
    """Nothing to run is not a failure: `ok` defaults to True for a test dict
    that never recorded a verdict."""
    assert round_succeeded(_round(tests={"ran": False, "ok": True})) is True


def test_main_exits_non_zero_for_a_failed_round(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The end-to-end contract: a driver sees a non-zero exit, not a clean 0
    with a failure payload on stdout."""
    monkeypatch.setattr(
        "agent_fleet.pr_owner.run_own",
        lambda **kwargs: _round(pushed=False, detail="engine failed: boom"),  # noqa: ARG005
    )
    code = main(["pr", "own", "--pr", "1", "--repo-path", "/tmp"])
    assert code == 1
    assert "engine failed: boom" in capsys.readouterr().out


def test_main_exits_zero_for_a_successful_round(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("agent_fleet.pr_owner.run_own", lambda **kwargs: _round())  # noqa: ARG005
    assert main(["pr", "own", "--pr", "1", "--repo-path", "/tmp"]) == 0
    assert "pushed" in capsys.readouterr().out


def test_main_exits_one_and_prints_one_line_on_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`run_own` never raises, so the error path prints to stderr and exits 1."""
    monkeypatch.setattr(
        "agent_fleet.pr_owner.run_own",
        lambda **kwargs: {"error": "gh pr view 999 failed"},  # noqa: ARG005
    )
    assert main(["pr", "own", "--pr", "999", "--repo-path", "/tmp"]) == 1
    captured = capsys.readouterr()
    assert "error: gh pr view 999 failed" in captured.err
    assert captured.err.count("\n") == 1


def test_cmd_pr_own_reads_the_repo_path_and_pr_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def _run_own(**kwargs: object) -> dict[str, object]:
        seen.update(kwargs)
        return _round()

    monkeypatch.setattr("agent_fleet.pr_owner.run_own", _run_own)
    main(["pr", "own", "--pr", "12", "--repo-path", "/tmp/repo", "--failing", "tests/a.py::t"])
    assert seen["pr_number"] == 12
    assert seen["failing"] == ["tests/a.py::t"]


def test_round_succeeded_tolerates_a_missing_tests_key() -> None:
    assert round_succeeded({"pushed": True, "new_head": "a"}) is True


def test_round_succeeded_ignores_a_non_dict_tests_value() -> None:
    """A `tests` value that is not a dict carries no verdict to distrust."""
    assert round_succeeded({"pushed": True, "tests": "not-a-dict"}) is True
