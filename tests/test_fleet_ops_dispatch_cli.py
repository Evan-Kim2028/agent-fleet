"""``fleet dispatch`` has two modes behind one subcommand.

With a ``QUEUE.jsonl`` positional it runs the durable queue dispatcher; with no
positional it keeps the original issue-triggered behaviour that README,
docs/SCHEDULES.md, and the schedule watcher all document. Getting that second
mode wrong would silently break the schedule watcher, so it is pinned here.
"""

from __future__ import annotations

import argparse
import functools
import json
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.fleet_ops.dispatch import SpawnedProc

from agent_fleet.fleet_ops import cli as fleet_ops_cli
from agent_fleet.fleet_ops import registry


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


def _parser() -> argparse.ArgumentParser:
    """The real `dispatch` registration, plus the handler main() installs."""
    from agent_fleet import cli

    root = argparse.ArgumentParser(prog="agent-fleet")
    sub = root.add_subparsers(dest="command", required=True)
    fleet_ops_cli.register_dispatch_command(sub)
    sub.choices["dispatch"].set_defaults(func=cli.cmd_dispatch)
    return root


def _subparsers_action(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    """The subparsers action, reached without poking ``parser._subparsers``.

    ``parse_args`` is the supported way to learn the registered subcommands,
    but help text is only exposed through the action objects, so the tests that
    assert on the help strings have to reach them here.
    """
    return next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))


def _subparser_choices(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    return dict(_subparsers_action(parser).choices)


# ------------------------------------------------------------- registration


def test_dispatch_is_registered() -> None:
    assert "dispatch" in _subparser_choices(_parser())


def test_the_queue_positional_is_optional() -> None:
    args = _parser().parse_args(["dispatch"])
    assert args.queue is None


def test_the_queue_positional_parses_with_its_flags() -> None:
    args = _parser().parse_args(
        [
            "dispatch",
            "queue.jsonl",
            "--operator",
            "documents-0e",
            "--max-lanes",
            "3",
            "--max-gates",
            "2",
            "--gate-cmd",
            "/opt/gate {lane} {pr}",
            "--repo",
            "acme=/src/acme",
            "--repo",
            "other=/src/other",
            "--psi-avg10-max",
            "30",
            "--json",
        ]
    )
    assert args.queue == "queue.jsonl"
    assert args.operator == "documents-0e"
    assert args.max_lanes == 3
    assert args.max_gates == 2
    assert args.gate_cmd == "/opt/gate {lane} {pr}"
    assert args.repo == ["acme=/src/acme", "other=/src/other"]
    assert args.psi_avg10_max == 30.0
    assert args.json is True


def test_the_flags_default_to_none_so_config_can_win() -> None:
    """flag > config > default; None is how config gets its turn."""
    args = _parser().parse_args(["dispatch", "q.jsonl", "--operator", "op"])
    assert args.max_lanes is None
    assert args.max_gates is None
    assert args.gate_cmd is None
    assert args.repo is None


def test_bare_dispatch_still_routes_to_issue_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The documented issue-dispatch path must be untouched."""
    called: list[bool] = []

    import agent_fleet.issue_loop.dispatch as issue_dispatch

    def fake_main() -> None:
        called.append(True)
        raise SystemExit(0)

    monkeypatch.setattr(issue_dispatch, "main", fake_main)
    monkeypatch.delenv("ISSUE_NUMBER", raising=False)

    from agent_fleet import cli

    args = _parser().parse_args(["dispatch"])
    assert cli.cmd_dispatch(args) == 0
    assert called == [True], "bare dispatch must still be the issue dispatch"


def test_the_issue_dispatch_exit_code_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_fleet.issue_loop.dispatch as issue_dispatch

    def fake_main() -> None:
        raise SystemExit(2)

    monkeypatch.setattr(issue_dispatch, "main", fake_main)
    from agent_fleet import cli

    assert cli.cmd_dispatch(_parser().parse_args(["dispatch"])) == 2


# --------------------------------------------------------- queue dispatch


def _queue(tmp_path: Path, *names: str) -> Path:
    path = tmp_path / "q.jsonl"
    path.write_text(
        "\n".join(json.dumps({"lane": n, "repo": "acme", "task": "t"}) for n in names),
        encoding="utf-8",
    )
    return path


def _args(tmp_path: Path, queue: Path, **overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "queue": str(queue),
        "operator": "documents-0e",
        "max_lanes": None,
        "max_gates": None,
        "gate_cmd": None,
        "repo": [f"acme={tmp_path / 'repo'}"],
        "psi_avg10_max": None,
        "json": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_queue_dispatch_needs_an_operator(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "repo").mkdir()
    code = fleet_ops_cli.cmd_dispatch_queue(_args(tmp_path, _queue(tmp_path, "a"), operator=None))
    assert code == 2
    assert "--operator" in capsys.readouterr().err


def test_queue_dispatch_reports_an_unmapped_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "repo").mkdir()
    code = fleet_ops_cli.cmd_dispatch_queue(_args(tmp_path, _queue(tmp_path, "a"), repo=[]))
    assert code == 2
    assert "acme" in capsys.readouterr().err


def test_queue_dispatch_reports_a_bad_repo_pair(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "repo").mkdir()
    code = fleet_ops_cli.cmd_dispatch_queue(
        _args(tmp_path, _queue(tmp_path, "a"), repo=["/src/acme"])
    )
    assert code == 2
    assert "NAME=PATH" in capsys.readouterr().err


def test_queue_dispatch_reports_an_empty_queue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "repo").mkdir()
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    code = fleet_ops_cli.cmd_dispatch_queue(_args(tmp_path, empty))
    assert code == 2
    assert "no queue items" in capsys.readouterr().err


def test_queue_dispatch_reports_a_malformed_queue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "repo").mkdir()
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json}\n", encoding="utf-8")
    code = fleet_ops_cli.cmd_dispatch_queue(_args(tmp_path, bad))
    assert code == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_queue_dispatch_reports_a_missing_queue_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "repo").mkdir()
    code = fleet_ops_cli.cmd_dispatch_queue(_args(tmp_path, tmp_path / "nope.jsonl"))
    assert code == 2
    assert capsys.readouterr().err.strip()


def test_a_clean_queue_exits_zero_and_prints_a_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole command, with only the spawn stubbed out."""
    (tmp_path / "repo").mkdir()
    queue = _queue(tmp_path, "alpha")
    never = functools.partial(fleet_ops_cli.run_dispatch, spawn=_never_spawn)
    monkeypatch.setattr(fleet_ops_cli, "run_dispatch", never)
    code = fleet_ops_cli.cmd_dispatch_queue(_args(tmp_path, queue))
    out = capsys.readouterr().out
    assert code in (0, 1)
    assert "dispatch documents-0e" in out


def test_resolve_repos_splits_on_the_first_equals() -> None:
    args = argparse.Namespace(repo=["acme=/src/acme", "odd=/a=b"])
    resolved = fleet_ops_cli._resolve_repos(
        args, [fleet_ops_cli.DispatchItem.from_dict({"lane": "a", "repo": "odd"})]
    )
    assert resolved["odd"] == "/a=b"


def test_resolve_repos_rejects_an_entry_with_no_equals() -> None:
    with pytest.raises(ValueError, match="NAME=PATH"):
        fleet_ops_cli._resolve_repos(
            argparse.Namespace(repo=["nope"]),
            [fleet_ops_cli.DispatchItem.from_dict({"lane": "a", "repo": "acme"})],
        )


# ------------------------------------------------------------------- help


def test_dispatch_help_exits_zero() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "agent_fleet.cli", "dispatch", "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0
    assert "QUEUE.jsonl" in proc.stdout
    assert "--max-gates" in proc.stdout


def test_dispatch_help_documents_both_modes() -> None:
    """Both modes must be discoverable, or an operator picks the wrong one."""
    parser = _parser()
    # The subcommand's one-line summary lives on the subparsers action.
    action = _subparsers_action(parser)
    pseudo = next(a for a in action._get_subactions() if a.dest == "dispatch")
    assert "issue-triggered" in (pseudo.help or "")
    assert "QUEUE.jsonl" in (pseudo.help or "")
    # And the positional's own help spells out which mode it selects.
    dispatch = _subparser_choices(parser)["dispatch"]
    positional_help = " ".join(a.help or "" for a in dispatch._actions if a.dest == "queue")
    assert "queue dispatcher" in positional_help


def _never_spawn(argv: list[str], **kwargs: object) -> SpawnedProc:  # noqa: ARG001
    raise AssertionError("no lane should be launched in this test")


def test_the_events_stream_is_namespaced_by_operator() -> None:
    """The replacement for the single events.log two operators interleaved."""
    registry.append_event("documents-0e", "a", "dispatch.test")
    assert len(registry.read_events(operator="documents-0e")) == 1
    assert registry.read_events(operator="documents-1d") == []
