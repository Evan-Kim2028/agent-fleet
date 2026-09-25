"""The ``lane`` / ``lanes`` CLI surface — registration, flags, and exit codes.

The commands are thin adapters, so what is worth testing is that the parser
exposes the documented flags, that the gate capability is resolved from the live
``sub.choices``, and that a refusal or an escalation is reported as a non-zero
exit rather than swallowed.
"""

from __future__ import annotations

import argparse
import json

import pytest

from agent_fleet.fleet_ops import cli, registry
from agent_fleet.fleet_ops.registry import STATE_APPROVED, STATE_RUNNING


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):  # noqa: ANN001, ANN202
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


def _parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="agent-fleet")
    sub = root.add_subparsers(dest="command", required=True)
    cli.register_lane_commands(sub)
    return root


# ---------------------------------------------------------------- registration


def _subparser_choices() -> dict[str, argparse.ArgumentParser]:
    """The registered subcommand names, via the subparsers action's public mapping."""
    group = _parser()._subparsers
    assert group is not None
    for action in group._group_actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("no subparsers action registered")


def test_lane_and_lanes_are_registered() -> None:
    assert set(_subparser_choices()) >= {"lane", "lanes"}


def test_lane_run_exposes_the_documented_flags() -> None:
    args = _parser().parse_args(
        [
            "lane",
            "run",
            "--operator",
            "documents-1d",
            "--lane",
            "movers",
            "--repo-path",
            "/repo",
            "--task-file",
            "/task.md",
            "--engine",
            "cmd",
            "--branch",
            "dq1d/movers",
            "--status-file",
            "/lane.status",
            "--expected-repo",
            "Evan-Kim2028/lake-of-rage",
        ]
    )
    assert args.operator == "documents-1d"
    assert args.lane == "movers"
    assert args.repo_path == "/repo"
    assert args.task_file == "/task.md"
    assert args.engine == "cmd"
    assert args.branch == "dq1d/movers"
    assert args.status_file == "/lane.status"
    assert args.expected_repo == "Evan-Kim2028/lake-of-rage"
    assert args.json is False


def test_engine_is_constrained_to_the_policy_engines() -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args(
            [
                "lane",
                "run",
                "--operator",
                "o",
                "--lane",
                "l",
                "--repo-path",
                "/r",
                "--task-file",
                "/t",
                "--engine",
                "grok",
            ]
        )


def test_required_flags_are_required() -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args(["lane", "run", "--operator", "o"])


def test_lanes_status_and_stop_parse() -> None:
    parser = _parser()
    status = parser.parse_args(["lanes", "status", "--all", "--json"])
    assert status.all is True and status.json is True
    stop = parser.parse_args(
        ["lanes", "stop", "movers", "--operator", "documents-0e", "--grace", "5"]
    )
    assert stop.lane == "movers"
    assert stop.operator == "documents-0e"
    assert stop.grace == 5.0


def test_the_gate_capability_is_resolved_at_parse_time() -> None:
    """`lane run` learns whether a gate exists from the live subcommand set."""
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="command", required=True)
    cli.register_lane_commands(sub)
    args = root.parse_args(
        ["lane", "run", "--operator", "o", "--lane", "l", "--repo-path", "/r", "--task-file", "/t"]
    )
    # No `gate` subcommand is registered by fleet_ops, so detection is False.
    assert args._known_subcommands is not None
    assert "gate" not in args._known_subcommands


# ------------------------------------------------------------------ handlers


def test_lanes_status_renders_the_table(capsys: pytest.CaptureFixture[str]) -> None:
    registry.update_record("documents-0e", "movers", state=STATE_RUNNING, repo="o/r", pr=7)
    code = cli.cmd_lanes_status(argparse.Namespace(operator=None, all=True, json=False))
    out = capsys.readouterr().out
    assert code == 0
    assert "LANE" in out and "movers" in out and "documents-0e" in out


def test_lanes_status_json(capsys: pytest.CaptureFixture[str]) -> None:
    registry.update_record("documents-1d", "beta", state=STATE_APPROVED, repo="o/r", pr=9)
    code = cli.cmd_lanes_status(argparse.Namespace(operator=None, all=True, json=True))
    rows = json.loads(capsys.readouterr().out)
    assert code == 0
    assert rows[0]["lane"] == "beta"
    assert rows[0]["pr"] == 9


def test_lanes_status_filters_by_operator(capsys: pytest.CaptureFixture[str]) -> None:
    registry.update_record("documents-0e", "alpha", state=STATE_APPROVED)
    registry.update_record("documents-1d", "beta", state=STATE_APPROVED)
    cli.cmd_lanes_status(argparse.Namespace(operator="documents-1d", all=False, json=True))
    rows = json.loads(capsys.readouterr().out)
    assert [r["lane"] for r in rows] == ["beta"]


def test_operator_and_all_together_is_an_error(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.cmd_lanes_status(argparse.Namespace(operator="documents-1d", all=True, json=False))
    assert code == 2
    assert "not both" in capsys.readouterr().err


def test_stopping_an_unknown_lane_exits_non_zero(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.cmd_lanes_stop(
        argparse.Namespace(lane="nope", operator=None, grace=10.0, json=False)
    )
    assert code == 1
    assert "refused to stop" in capsys.readouterr().err


def test_stopping_never_reports_success_for_a_refusal(capsys: pytest.CaptureFixture[str]) -> None:
    """A refusal must be visible in the exit code, not just on stderr."""
    registry.update_record("documents-0e", "shared", state="running", pid=1, pgid=1)
    registry.update_record("documents-1d", "shared", state="running", pid=1, pgid=1)
    code = cli.cmd_lanes_stop(
        argparse.Namespace(lane="shared", operator=None, grace=1.0, json=True)
    )
    assert code == 1
    assert json.loads(capsys.readouterr().out)["stopped"] is False
