"""contract_2: the gate must be told where to write its verdict.

Claim under test
----------------
``gate_argv()`` has no ``status_file`` parameter and never emits ``--status-file``,
so the gate runs with ``status_file=None`` (``cmd_gate`` reads
``getattr(args, 'status_file', None)``). Its APPROVED / NEEDS-ESCALATION verdict
is therefore never written to the file ``_reap_gate`` -> ``_status_for`` reads
back, and every gate result becomes an unconditional ``escalated (no approval
line)`` -- even for a PR the gate approves.

``_launch_lane`` already does the right thing for ``lane run`` (it records
``out_root/lanes/<lane>.status`` and passes ``--status-file``), so the dispatcher
knows exactly which file the verdict belongs in. The gate is simply never told.

The gate stand-in below cooperates correctly: it appends its verdict to the
status file it is *given*, and if it is given none it has nowhere to write. That
is the real ``agent-fleet gate`` contract, so a passing run is only possible if
the dispatcher passes the file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet.fleet_ops import pressure
from agent_fleet.fleet_ops.dispatch import run_dispatch

if TYPE_CHECKING:
    from collections.abc import Sequence

IDLE = pressure.Throttle(some_avg10=0.0, path=Path("/fake/cpu.pressure"), available=True)


class _FakeProc:
    """A child that exits after *polls* ``poll()`` calls (0 = immediately)."""

    def __init__(self, pid: int, exit_code: int | None = 0, *, polls: int = 0) -> None:
        self.pid = pid
        self.returncode = exit_code
        self._polls_left = polls

    def poll(self) -> int | None:
        if self._polls_left > 0:
            self._polls_left -= 1
            return None
        return self.returncode


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


class _Dispatch:
    """One ``run_dispatch`` over a single-lane queue, with a cooperating gate."""

    def __init__(self, tmp_path: Path) -> None:
        self.out = tmp_path / "out"
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        self.queue = tmp_path / "q.jsonl"
        self.queue.write_text(
            json.dumps({"lane": "alpha", "repo": "acme", "task": "t"}), encoding="utf-8"
        )
        self.spawned: list[list[str]] = []
        self.status_writes: list[str] = []
        self._pid = 1000

    def _spawn(self, argv: Sequence[str], **kwargs: Any) -> Any:  # noqa: ANN401
        argv = list(argv)
        self.spawned.append(argv)
        self._pid += 1
        is_lane = argv[:3] == ["fleet", "lane", "run"]
        if is_lane:
            # The lane guarantees a PR and reports it on its --json output,
            # written through the log handle the dispatcher handed spawn.
            with kwargs["stdout"] as log:
                log.write('{"state": "pr_guaranteed", "pr": 9, "worktree": "/w"}')
            return _FakeProc(self._pid, 0, polls=1)

        # A correct gate: it appends its verdict to the status file it was given,
        # and has nowhere to write when it was given none.
        if "--status-file" in argv:
            target = Path(argv[argv.index("--status-file") + 1])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("12:00:00 PREMERGE-APPROVED abc1234def\n", encoding="utf-8")
            self.status_writes.append(str(target))
        return _FakeProc(self._pid, 0)

    def run(self) -> Any:  # noqa: ANN401
        return run_dispatch(
            operator="documents-0e",
            queue_path=self.queue,
            repos={"acme": str(self.repo)},
            max_lanes=2,
            max_gates=2,
            gate_cmd=None,  # the documented default: the built-in gate
            spawn=self._spawn,
            psi_reader=lambda: IDLE,
            sleep=lambda _s: None,
            run_dir=self.out,
        )

    def gate_argvs(self) -> list[list[str]]:
        return [a for a in self.spawned if a[:2] == ["agent-fleet", "gate"]]


def test_the_gate_is_told_which_status_file_to_write_its_verdict_to(tmp_path: Path) -> None:
    """``--status-file`` must be on the gate argv; it is the only verdict channel.

    ``_reap_gate`` classifies a gate from ``_status_for(lane)``, i.e. from the
    lane's recorded status file. If the gate is never handed that path it cannot
    write where the dispatcher will look.
    """
    run = _Dispatch(tmp_path)
    run.run()

    gate_argvs = run.gate_argvs()
    assert gate_argvs, "a lane that produced a PR must get a gate"
    for argv in gate_argvs:
        assert "--status-file" in argv, (
            f"the gate argv {argv} carries no --status-file, so the gate's "
            f"APPROVED/NEEDS-ESCALATION line can never reach the dispatcher"
        )


def test_a_gate_that_approves_yields_approved(tmp_path: Path) -> None:
    """End to end: a gate that exits 0 and writes PREMERGE-APPROVED is approved.

    The gate here is doing everything right -- it approves and exits 0. The only
    thing that can make the dispatcher report an escalation is failing to give
    the gate the file it is supposed to write.
    """
    run = _Dispatch(tmp_path)
    summary = run.run()

    assert run.status_writes, "the gate was never asked to record a verdict"
    assert summary.approved == 1, (
        f"an approving gate was reported as {summary.to_dict()}; the verdict the "
        f"gate wrote was never read back"
    )
    assert summary.escalated == 0, f"summary: {summary.to_dict()}"
    assert summary.exit_code() == 0, f"summary: {summary.to_dict()}"
