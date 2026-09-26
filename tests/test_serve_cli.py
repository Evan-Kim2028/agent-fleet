"""The `fleet serve` CLI surface: registration, handlers and exit codes.

Two things this file exists to pin beyond the obvious.

**Registration.** ``serve`` must appear in the subparser choices *before*
``cli_core.normalize_argv`` runs, because that function rewrites argv against
the live set of known subcommands. Register too late and `fleet serve` gets
mangled into something else — a baffling failure whose cause is invisible from
the symptom.

**``--config``.** ``serve`` deliberately does not register its own. argparse
resolves a subparser's default onto the same ``dest`` as the top-level parser,
so a second ``--config`` would silently discard the value passed as
``fleet --config X serve`` and fall back to the global fleet.yaml. A supervisor
quietly running on thresholds the operator did not choose is worse than an
error, so the top-level flag is read as-is and the override is spelled
``--serve-config``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from agent_fleet import cli as top_cli
from agent_fleet.serve import cli as serve_cli


def _parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="agent-fleet")
    sub = root.add_subparsers(dest="command", required=True)
    serve_cli.register_serve_commands(sub)
    return root


def _subcommands(root: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    # argparse's subparsers attribute is private and typed as optional, so it
    # is narrowed once here rather than at every use.
    subparsers = root._subparsers
    assert subparsers is not None
    for action in subparsers._group_actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("no subparsers found")


# --------------------------------------------------------------- registration


def test_serve_is_registered() -> None:
    assert "serve" in _subcommands(_parser())


def test_serve_has_every_documented_subcommand() -> None:
    serve = _subcommands(_parser())["serve"]
    assert set(_subcommands(serve)) == {
        "run",
        "status",
        "stop",
        "watchdog",
        "decisions",
        "capacity",
    }


def test_serve_appears_before_normalize_argv_runs() -> None:
    """Registration order matters and the failure is invisible from the symptom."""
    from agent_fleet.cli_core import normalize_argv

    choices = {"lane", "gate", "serve", "status"}
    argv = normalize_argv(["fleet", "serve"], choices, Path.cwd())
    assert "serve" in " ".join(argv), f"normalize_argv mangled serve: {argv}"


def test_no_serve_subcommand_registers_its_own_config_flag() -> None:
    """A second --config would shadow the top-level one and be silently dropped."""
    serve = _subcommands(_parser())["serve"]
    for name, leaf in _subcommands(serve).items():
        dests = {action.dest for action in leaf._actions}
        assert "config" not in dests, (
            f"{name} redefines --config and would shadow the top-level one"
        )
        assert "serve_config" in dests, f"{name} must expose the --serve-config override"


def test_top_level_config_is_not_shadowed_by_serve() -> None:
    root = argparse.ArgumentParser(prog="agent-fleet")
    root.add_argument("--config", default=None)
    sub = root.add_subparsers(dest="command", required=True)
    serve_cli.register_serve_commands(sub)
    args = root.parse_args(["--config", "fleet.yaml", "serve"])
    assert args.config == "fleet.yaml", "the top-level value must survive into the subcommand"


# ------------------------------------------------------------------- handlers


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path / "home" / "runs"))
    monkeypatch.chdir(tmp_path)


def _config_file(tmp_path: Path, body: str = "") -> Path:
    path = tmp_path / "serve.yaml"
    path.write_text(body or "serve:\n  tick_seconds: 0.01\n", encoding="utf-8")
    return path


def test_status_requires_an_operator(capsys: pytest.CaptureFixture[str]) -> None:
    args = argparse.Namespace(
        operator="", serve_config=None, config=None, repo_root=None, json=False
    )
    assert serve_cli.cmd_serve_status(args) == 2
    assert "--operator is required" in capsys.readouterr().out


def test_run_requires_an_operator(capsys: pytest.CaptureFixture[str]) -> None:
    args = argparse.Namespace(
        operator="",
        serve_config=None,
        config=None,
        repo_root=None,
        max_ticks=1,
        dry_run_watchdog=False,
    )
    assert serve_cli.cmd_serve_run(args) == 2
    assert "--operator is required" in capsys.readouterr().out


def test_a_serve_config_without_a_serve_section_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "plain.yaml"
    path.write_text("default_model: composer\n", encoding="utf-8")
    args = argparse.Namespace(
        operator="op", serve_config=str(path), config=None, repo_root=None, json=False
    )
    assert serve_cli.cmd_serve_status(args) == 2
    assert "no `serve:`" in capsys.readouterr().out


def test_status_json_is_the_same_dict_the_text_renders(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agent_fleet.serve.clock import FakeClock
    from agent_fleet.serve.items import STAGE_QUEUED, ItemBoard
    from agent_fleet.serve.paths import items_path

    operator = "op"
    clock = FakeClock()
    board = ItemBoard(items_path(operator), clock=clock)
    board.record("lane-1", STAGE_QUEUED, repo="lake-of-rage")

    args = argparse.Namespace(
        operator=operator,
        serve_config=str(_config_file(tmp_path)),
        config=None,
        repo_root=str(tmp_path),
        json=True,
    )
    assert serve_cli.cmd_serve_status(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["operator"] == operator
    assert {s["stage"] for s in payload["stages"]} >= {"queued", "gating", "merged"}
    queued = next(s for s in payload["stages"] if s["stage"] == "queued")
    assert queued["depth"] == 1
    assert queued["oldest_item"] == "lane-1"
    assert queued["wait_reason"]


def test_status_text_renders_the_screen(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = argparse.Namespace(
        operator="op",
        serve_config=str(_config_file(tmp_path)),
        config=None,
        repo_root=str(tmp_path),
        json=False,
    )
    assert serve_cli.cmd_serve_status(args) == 0
    out = capsys.readouterr().out
    for section in ("COMPONENTS", "CAPACITY", "STAGES", "dispatcher", "queue depth"):
        assert section in out


def test_watchdog_is_a_dry_run_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = argparse.Namespace(
        operator="op",
        serve_config=str(_config_file(tmp_path)),
        config=None,
        repo_root=str(tmp_path),
        apply=False,
        json=False,
    )
    assert serve_cli.cmd_serve_watchdog(args) == 0
    out = capsys.readouterr().out
    assert "WOULD ACT" in out, "this process kills things; the default must be read-only"


def test_watchdog_json_reports_rules_and_deferrals(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = argparse.Namespace(
        operator="op",
        serve_config=str(_config_file(tmp_path)),
        config=None,
        repo_root=str(tmp_path),
        apply=False,
        json=True,
    )
    assert serve_cli.cmd_serve_watchdog(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert set(payload) == {"operator", "dry_run", "remediations", "deferred", "by_rule"}


def test_decisions_lists_the_queue(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from agent_fleet.serve.clock import FakeClock
    from agent_fleet.serve.escalate import EscalationRouter
    from agent_fleet.serve.paths import decisions_path

    router = EscalationRouter("op", clock=FakeClock())
    router.route("lane-9", "fenced: do not edit print_identity.py", pr=3541)

    args = argparse.Namespace(
        operator="op",
        serve_config=str(_config_file(tmp_path)),
        config=None,
        repo_root=str(tmp_path),
        json=False,
    )
    assert serve_cli.cmd_serve_decisions(args) == 0
    out = capsys.readouterr().out
    assert "lane-9" in out
    assert "fence" in out
    assert decisions_path("op").exists()


def test_decisions_with_an_empty_queue_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = argparse.Namespace(
        operator="op",
        serve_config=str(_config_file(tmp_path)),
        config=None,
        repo_root=str(tmp_path),
        json=False,
    )
    assert serve_cli.cmd_serve_decisions(args) == 0
    assert "no decisions pending" in capsys.readouterr().out


def test_capacity_prints_the_published_file(capsys: pytest.CaptureFixture[str]) -> None:
    args = argparse.Namespace(
        operator="op", serve_config=None, config=None, repo_root=None, json=False
    )
    assert serve_cli.cmd_serve_capacity(args) == 1
    assert "no capacity file" in capsys.readouterr().out


def test_stop_with_no_supervisor_reports_it(capsys: pytest.CaptureFixture[str]) -> None:
    args = argparse.Namespace(
        operator="op", serve_config=None, config=None, repo_root=None, max_ticks=0
    )
    assert serve_cli.cmd_serve_stop(args) == 1
    assert "no supervisor recorded" in capsys.readouterr().out


def test_run_refuses_dry_run_watchdog(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = argparse.Namespace(
        operator="op",
        serve_config=str(_config_file(tmp_path)),
        config=None,
        repo_root=str(tmp_path),
        max_ticks=1,
        dry_run_watchdog=True,
    )
    assert serve_cli.cmd_serve_run(args) == 2
    assert "serve watchdog" in capsys.readouterr().err


def test_run_executes_a_bounded_number_of_ticks(tmp_path: Path) -> None:
    """The end-to-end path: a real supervisor, real children, real capacity file."""
    body = (
        "serve:\n"
        "  tick_seconds: 0.01\n"
        "  shutdown_grace_s: 1\n"
        "  components:\n"
        f'    janitor:\n      command: "{Path(__file__).parent and "true"}"\n'
    )
    args = argparse.Namespace(
        operator="op",
        serve_config=str(_config_file(tmp_path, body)),
        config=None,
        repo_root=str(tmp_path),
        max_ticks=2,
        dry_run_watchdog=False,
    )
    assert serve_cli.cmd_serve_run(args) == 0
    from agent_fleet.serve.paths import capacity_path, pid_path

    assert capacity_path("op").exists()
    assert not pid_path("op").exists(), "the pid file must be cleaned up on exit"


def test_the_real_parser_accepts_fleet_serve() -> None:
    """`fleet serve --help` must work against the shipped parser."""
    with pytest.raises(SystemExit) as excinfo:
        top_cli.main(["serve", "--help"])
    assert excinfo.value.code == 0
