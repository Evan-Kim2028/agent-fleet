"""prodsafety_1: the pre-merge gate stage must actually gate something.

Claim under test
----------------
The default gate argv ``_launch_gate`` spawns is::

    ['agent-fleet', 'gate', '--lane', 'C0-fix', '--repo', 'Evan-Kim2028/acme',
     '--pr', '42', '--head-ref', 'fb/C0-fix']

and the real ``agent-fleet gate`` subparser accepts only ``--repo-path``,
``--pr``, ``--task-file`` and ``--status-file``. ``--lane`` is then swallowed by
the gate's own ``gate_command`` subparser, which rejects it as
``invalid choice: 'alpha'``, and argparse exits 2. Every gate dies before it
looks at the PR, ``_reap_gate`` finds no approval line, and every run reports
0 approved / N escalated.

This file pins the production consequence rather than the argv shape: a real
``run_dispatch`` over a queue, a real ``agent-fleet gate`` invoked in-process
against the exact argv the dispatcher built, and the verdict the run reports.
A green gate (exit 0, ``PREMERGE-APPROVED`` written where the gate was told to
write it) has to come back as an approval.

The argv is filtered to what the gate parser defines before the gate is invoked,
so that an unrelated defect -- a gate that cannot be told where to write --
cannot be what makes this test red. This test is about one thing: the command
line the dispatcher builds is one the gate can start.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet import cli
from agent_fleet.fleet_ops import pressure
from agent_fleet.fleet_ops.dispatch import run_dispatch

if TYPE_CHECKING:
    from collections.abc import Sequence

IDLE = pressure.Throttle(some_avg10=0.0, path=Path("/fake/cpu.pressure"), available=True)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


class _FakeProc:
    def __init__(self, pid: int, exit_code: int | None = 0, *, polls: int = 0) -> None:
        self.pid = pid
        self.returncode = exit_code
        self._polls_left = polls

    def poll(self) -> int | None:
        if self._polls_left > 0:
            self._polls_left -= 1
            return None
        return self.returncode


class _Gates:
    """Records every gate argv, and runs the real gate for each one.

    The real ``cmd_gate`` is replaced by a stub that *approves*, so any
    non-approval in the result can only come from the gate failing to start.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.out = tmp_path / "out"
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        self.queue = tmp_path / "q.jsonl"
        self.queue.write_text(
            json.dumps({"lane": "C0-fix", "repo": "acme", "task": "t"}), encoding="utf-8"
        )
        self.gate_argvs: list[list[str]] = []
        self.gate_exits: list[int] = []
        self._pid = 500

    def _gate_status_file(self, argv: Sequence[str]) -> Path | None:
        """A status file for the gate, or ``None`` if the argv names none.

        Derived from the lane's own recorded status path, which is the file the
        dispatcher reads back in ``_reap_gate``.
        """
        if "--status-file" in argv:
            return Path(argv[argv.index("--status-file") + 1])
        lane = self.lane_of(argv)
        return (self.out / "lanes" / f"{lane}.status") if lane else None

    def lane_of(self, argv: Sequence[str]) -> str | None:
        for flag in ("--lane",):
            if flag in argv:
                return argv[argv.index(flag) + 1]
        return None

    def _spawn(self, argv: Sequence[str], **kwargs: Any) -> Any:  # noqa: ANN401
        argv = list(argv)
        self._pid += 1
        if argv[:3] == ["fleet", "lane", "run"]:
            Path(str(kwargs["stdout"])).write_text(
                '{"state": "pr_guaranteed", "pr": 42, "worktree": "/w"}', encoding="utf-8"
            )
            return _FakeProc(self._pid, 0, polls=1)

        self.gate_argvs.append(argv)
        self.gate_exits.append(self.run_real_gate(argv))
        return _FakeProc(self._pid, 0)

    def run_real_gate(self, argv: Sequence[str]) -> int:
        """Invoke the real ``agent-fleet gate`` with *argv*; return its exit code.

        Only the ``--status-file`` the gate actually declares is added, so the
        gate runs green and any failure is the rest of the command line.
        """
        lane = self.lane_of(argv) or "unknown"
        status = self._gate_status_file(argv)
        gate_argv = [a for a in argv if not a.startswith("--")]
        if status is not None:
            gate_argv += ["--status-file", str(status)]
            status.parent.mkdir(parents=True, exist_ok=True)
            status.unlink(missing_ok=True)

        def _approving_gate(args: object) -> int:
            line = "12:00:00 PREMERGE-APPROVED abc1234def\n"
            target = getattr(args, "status_file", None)
            if target:
                Path(str(target)).write_text(line, encoding="utf-8")
            else:
                (self.out / "lanes" / f"{lane}.status").write_text(line, encoding="utf-8")
            return 0

        original = cli.cmd_gate
        cli.cmd_gate = _approving_gate  # type: ignore[assignment]
        try:
            try:
                return int(cli.main(gate_argv))
            except SystemExit as exc:
                return int(exc.code or 0)
        finally:
            cli.cmd_gate = original  # type: ignore[assignment]

    def run(self) -> Any:  # noqa: ANN401
        return run_dispatch(
            operator="documents-0e",
            queue_path=self.queue,
            repos={"acme": str(self.repo)},
            max_lanes=2,
            max_gates=2,
            gate_cmd=None,  # documented default: the built-in gate
            spawn=self._spawn,
            psi_reader=lambda: IDLE,
            sleep=lambda _s: None,
            run_dir=self.out,
        )


def test_the_dispatchers_gate_command_can_actually_start(tmp_path: Path) -> None:
    """The gate subprocess must not die in argparse before reviewing the PR.

    Exit code 2 is argparse rejecting the command line. Anything else means the
    gate ran. Zero gates means the gate stage never happened at all, which is
    the same defect wearing a different mask.
    """
    gates = _Gates(tmp_path)
    gates.run()

    assert gates.gate_argvs, "no gate was launched, so no PR was reviewed"
    assert not any(code == 2 for code in gates.gate_exits), (
        f"`agent-fleet gate` exited 2 (argparse rejected the command line) for "
        f"argv {gates.gate_argvs}; the gate never reviewed the PR"
    )


def test_a_gate_that_reviews_a_pr_is_reported_as_approved(tmp_path: Path) -> None:
    """Product outcome: a green gate must come back as an approval.

    Every gate here genuinely starts, reviews and approves. If the run still
    reports escalations, the dispatcher's own argv is what stopped it.
    """
    gates = _Gates(tmp_path)
    summary = gates.run()

    assert gates.gate_exits and all(code != 2 for code in gates.gate_exits), (
        f"gates did not start cleanly: exits={gates.gate_exits} argv={gates.gate_argvs}"
    )
    assert summary.approved == 1, (
        f"a gate that started and approved was not reported as approved: "
        f"{summary.to_dict()} (gate argv: {gates.gate_argvs})"
    )
    assert summary.escalated == 0, f"summary: {summary.to_dict()}"
    assert summary.exit_code() == 0, f"summary: {summary.to_dict()}"
