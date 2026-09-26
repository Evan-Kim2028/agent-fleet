"""Regression tests for the PR #121 review blockers.

One test per claim, each written to fail against the pre-fix code:

* ``test_a_template_gate_command_emits_only_flags_the_gate_parser_defines``
  — contract-4 / spec-2: ``gate_argv`` appended ``--worktree`` to every custom
  ``--gate-cmd`` template, but no gate subcommand defines ``--worktree``, so
  argparse aborted the gate with exit 2 and ``_reap_gate`` reported the lane as
  ``escalated (gate exit 2)`` — a gate that never started, dressed as a rejected
  PR.
* ``test_the_lane_log_is_handed_to_spawn_as_a_path_not_a_Path`` — prodsafety-1:
  both spawn sites passed a ``Path`` as ``stdout``. Real ``Popen`` calls
  ``.fileno()`` on it and raises ``AttributeError``, so every real dispatch
  failed to launch anything; only the fakes, which accept a Path, kept CI green.
* ``test_an_scp_style_ssh_origin_yields_the_owners_slug`` — prodsafety-2 /
  spec-1: ``origin_slug`` stripped the ``user@`` only when it appeared before a
  ``/``, which the scp-style form ``git@github.com:owner/repo.git`` never does,
  so it returned ``github.com:owner/repo`` and the gate refused every SSH
  checkout's own origin as a mismatch.
* ``test_documented_tick_seconds_flag_is_registered`` — contract-5:
  ``--tick-seconds`` is documented for ``fleet dispatch`` but was never
  registered, so the documented flag was a hard argparse error and no
  dispatch-loop interval could be configured at all.
* ``test_dispatch_config_comes_from_the_repos_the_queue_names`` — contract-6:
  ``cmd_dispatch_queue`` loaded the config from ``Path.cwd()`` rather than the
  repos named by ``--repo``, so the whole ``fleet_ops.dispatch:`` block was
  ignored whenever the dispatcher was not run from the repo's own root.
* ``test_a_goal_starting_with_a_program_name_is_not_truncated`` —
  correctness-1: ``normalize_argv`` dropped the leading word of a multi-word
  goal whenever that word was a console-script name, because it could not tell
  a subprocess argv[0] from a goal's first word.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet.cli import cmd_gate
from agent_fleet.cli_core import normalize_argv
from agent_fleet.fleet_ops.cli import _dispatch_config, _tick_seconds
from agent_fleet.fleet_ops.dispatch import gate_argv, run_dispatch
from agent_fleet.gate import gitops
from agent_fleet.gate.gitops import GateError, origin_slug

if TYPE_CHECKING:
    from collections.abc import Sequence

PROGRAM_NAMES = ("fleet", "agent-fleet", "agent_fleet", "fleet.py")


def _parser() -> argparse.ArgumentParser:
    """The real ``dispatch`` registration, as ``main`` wires it up."""
    from agent_fleet.fleet_ops import cli as fleet_ops_cli

    root = argparse.ArgumentParser(prog="agent-fleet")
    sub = root.add_subparsers(dest="command", required=True)
    fleet_ops_cli.register_dispatch_command(sub)
    return root


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


@pytest.mark.parametrize("program", PROGRAM_NAMES)
def test_a_template_gate_command_emits_only_flags_the_gate_parser_defines(
    program: str,
) -> None:
    """A custom gate template must not gain a flag the gate parser rejects."""
    argv = gate_argv(
        f"{program} {{lane}} {{repo}} {{pr}}",
        lane="lane-0",
        pr=7,
        repo="acme",
        worktree="/repo/wt",
    )

    assert "--worktree" not in argv, (
        f"gate template argv {argv} carries --worktree, which the gate subparser "
        "does not define; argparse aborts with exit 2 before the gate reviews "
        "anything and the lane is reported escalated"
    )
    assert argv == [program, "lane-0", "acme", "7"]


def test_a_template_gate_command_can_name_the_worktree() -> None:
    """The worktree stays reachable, as a placeholder the operator controls."""
    argv = gate_argv(
        "/opt/fbgate {lane} {worktree} --pr {pr}",
        lane="lane-0",
        pr=7,
        repo="acme",
        worktree="/repo/wt",
    )

    assert argv == ["/opt/fbgate", "lane-0", "/repo/wt", "--pr", "7"]


def test_popen_rejects_a_path_or_a_str_but_accepts_a_file_handle() -> None:
    """Ground the spawn fix in ``subprocess`` itself, not a fake.

    ``Popen`` calls ``.fileno()`` on whatever ``stdout`` is, so a ``Path`` and a
    ``str`` both raise ``AttributeError``. Only an open handle works. Proving it
    here is the point: every existing test injected a fake ``spawn`` that
    accepted a ``Path``, which is why this reached production with CI green.
    """
    for bad in (Path("/home/evan/fleet/tmp/prodsafety-probe.log"), "/home/evan/fleet/tmp/p.log"):
        with pytest.raises(AttributeError):
            subprocess.Popen(
                [sys.executable, "-c", "pass"],
                stdout=bad,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    log = Path("/home/evan/fleet/tmp/prodsafety-probe.log")
    with log.open("w", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            [sys.executable, "-c", "print('hi')"],
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    assert proc.wait(timeout=60) == 0
    assert log.read_text(encoding="utf-8").strip() == "hi"


def test_a_launched_lane_spawns_with_a_real_file_handle(tmp_path: Path) -> None:
    """The dispatcher's own spawn path must hand ``Popen`` a ``fileno()``."""
    seen: list[object] = []

    class _Proc:
        def __init__(self, pid: int, polls: int) -> None:
            self.pid = pid
            self.returncode = 0
            self._polls_left = polls

        def poll(self) -> int | None:
            if self._polls_left > 0:
                self._polls_left -= 1
                return None
            return self.returncode

    _pid = 5000

    def spawn(argv: Sequence[str], **kwargs: Any) -> Any:  # noqa: ANN401
        nonlocal _pid
        _pid += 1
        argv = list(argv)
        stdout = kwargs["stdout"]
        seen.append(stdout)
        # Exactly what subprocess.Popen does with the value it is given: a Path
        # and a str both raise here, and only a real file object survives.
        stdout.fileno()
        is_lane = argv[:3] == ["fleet", "lane", "run"]
        if is_lane:
            with stdout as log:
                log.write('{"state": "no_pr"}')
        return _Proc(_pid, 0 if not is_lane else 1)

    repo = tmp_path / "repo"
    repo.mkdir()
    queue = tmp_path / "q.jsonl"
    queue.write_text(json.dumps({"lane": "lane-0", "repo": "acme", "task": "t0"}), encoding="utf-8")

    run_dispatch(
        operator="documents-0e",
        queue_path=queue,
        repos={"acme": str(repo)},
        max_lanes=2,
        max_gates=1,
        spawn=spawn,
        sleep=lambda _s: None,
        run_dir=tmp_path / "out",
    )

    assert seen, "no lane was spawned; the run never reached _launch_lane"
    assert all(hasattr(handle, "fileno") for handle in seen), (
        f"spawn was handed {seen!r}; a Path or a str makes Popen raise "
        "AttributeError and every real dispatch fails to launch"
    )


def test_an_scp_style_ssh_origin_yields_the_owners_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``git@github.com:owner/repo.git`` is the common SSH clone form."""
    monkeypatch.setattr(
        gitops,
        "_run_git",
        lambda *_a, **_k: "git@github.com:Evan-Kim2028/agent-fleet.git",
    )

    slug = origin_slug(Path())

    assert slug == "Evan-Kim2028/agent-fleet", (
        f"origin_slug returned {slug!r}; the scp-style form has no '/' before the "
        "':' so the userinfo was never stripped, and the cross-check then refuses "
        "a checkout's own origin"
    )


def test_a_host_only_origin_is_not_a_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    """A remote with no owner/repo must not name the host as the owner."""
    monkeypatch.setattr(gitops, "_run_git", lambda *_a, **_k: "https://github.com/onlyowner")

    assert origin_slug(Path()) == ""


def test_documented_tick_seconds_flag_is_registered() -> None:
    """``--tick-seconds`` is documented for ``fleet dispatch``; it must parse."""
    parser = _parser()
    args = parser.parse_args(["dispatch", "q.jsonl", "--operator", "op", "--tick-seconds", "5"])

    assert args.tick_seconds == 5.0
    assert _tick_seconds(args) == 5.0


def test_tick_seconds_defaults_and_rejects_a_non_positive_interval() -> None:
    from agent_fleet.fleet_ops.dispatch import DEFAULT_TICK_SECONDS

    assert _tick_seconds(argparse.Namespace(tick_seconds=None)) == DEFAULT_TICK_SECONDS
    with pytest.raises(ValueError, match="greater than 0"):
        _tick_seconds(argparse.Namespace(tick_seconds=0.0))


def test_dispatch_config_comes_from_the_repos_the_queue_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-repo ``fleet_ops.dispatch:`` tuning must apply from any cwd."""
    repo = tmp_path / "acme"
    repo.mkdir()
    (repo / ".agent-fleet.yaml").write_text(
        "fleet_ops:\n  dispatch:\n    max_gates: 1\n    psi_path: /custom/cpu.pressure\n",
        encoding="utf-8",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    config = _dispatch_config({"acme": str(repo)})

    assert config.dispatch.max_gates == 1, (
        f"max_gates resolved to {config.dispatch.max_gates}, not the repo's 1; "
        "the config was read from the cwd instead of the repo being worked on"
    )
    assert config.dispatch.psi_path == "/custom/cpu.pressure"


def test_dispatch_config_falls_back_to_the_cwd(tmp_path: Path) -> None:
    """A repo-less invocation keeps working exactly as it did before."""
    repo = tmp_path / "acme"
    repo.mkdir()
    config = _dispatch_config({"acme": str(repo)})

    from agent_fleet.fleet_ops.config import FleetOpsConfig

    assert config.dispatch.max_gates == FleetOpsConfig().dispatch.max_gates


def test_a_goal_starting_with_a_program_name_is_not_truncated() -> None:
    """Rule 4 must preserve the whole goal, not delete its first word."""
    known = {"run", "gate", "dispatch", "doctor", "lanes"}

    assert normalize_argv(["fleet", "ops", "restart"], known, Path("/tmp")) == [
        "run",
        "fleet",
        "ops",
        "restart",
    ]
    assert normalize_argv(["fleet"], known, Path("/tmp")) == ["run", "fleet"]


def test_a_subprocess_argv_still_has_its_program_name_dropped() -> None:
    """The case rule 0 exists for: a real spawn list keeps working."""
    known = {"run", "gate", "dispatch", "doctor", "lanes"}

    for program in PROGRAM_NAMES:
        assert normalize_argv([program, "gate", "--pr", "42"], known, Path("/tmp")) == [
            "gate",
            "--pr",
            "42",
        ]


def test_an_unresolvable_pr_is_an_escalation_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A cross-check that cannot resolve the PR must return 1, not raise.

    ``cross_check_gate_target`` calls ``resolve_pull_request``, which raises a
    plain ``GateError`` on no-gh/no-auth/no-remote/no-such-PR. The handler caught
    only ``GateTargetMismatch``, so the exception escaped ``cmd_gate`` and the
    automerge wrapper got neither the status line nor exit 1.
    """
    from agent_fleet.gate import pipeline

    def boom(*_a: object, **_k: object) -> object:
        raise GateError("gh CLI not found; cannot resolve the PR head")

    monkeypatch.setattr(gitops, "resolve_pull_request", boom)
    monkeypatch.setattr(
        pipeline,
        "run_gate",
        lambda **_k: pytest.fail("run_gate must not be reached"),
    )
    args = argparse.Namespace(
        gate_command=None,
        pr=999,
        repo_path="/tmp",
        repo=None,
        head_ref="fb/foo",
        task_file=None,
        status_file=None,
        config=None,
        judge_engine=None,
        lane_slug=None,
    )

    rc = cmd_gate(args)

    out = capsys.readouterr()
    assert rc == 1, f"expected exit 1, got {rc}"
    assert "NEEDS-ESCALATION" in out.err, (
        f"no NEEDS-ESCALATION status line was emitted (stderr={out.err!r}); the "
        "automerge wrapper gates on that line"
    )
