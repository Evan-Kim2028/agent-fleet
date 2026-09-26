"""prodsafety_2: an approving gate must not be read back as a rejection.

Claim under test
----------------
The dispatcher runs each lane as ``fleet lane run --no-gate`` and passes it
``--status-file out_root/lanes/<lane>.status``. With the gate disabled, the
runner appends its own line to that file::

    HH:MM:SS NEEDS-ESCALATION PR #42 guaranteed; gate disabled (--no-gate)...

Then ``_launch_gate`` spawns the real gate **without** ``--status-file``, so the
gate has nowhere to write and cannot append ``PREMERGE-APPROVED``. ``_reap_gate``
reads the lane's status file back and classifies it -- finding the stale
``NEEDS-ESCALATION`` the ``--no-gate`` lane wrote -- and reports the run as an
escalation with ``exit_code() == 1``.

The safety consequence: a green gate becomes indistinguishable from a real
rejection, so an approving PR is routed for human escalation and the automated
pre-merge gate is silently a no-op.

This test drives the full loop with a real gate process -- the dispatcher
decides whether it reviews, and the gate itself decides the verdict -- and
asserts on the classification the operator actually sees.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet import cli
from agent_fleet.fleet_ops import pressure
from agent_fleet.fleet_ops.dispatch import classify_status, run_dispatch
from agent_fleet.fleet_ops.statusfile import append_status, approved_line, escalation_line

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


class _Loop:
    """A full dispatch where the gate is a real, approving ``agent-fleet gate``."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.out = tmp_path / "out"
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        self.lane = "C0-fix"
        self.pr = 42
        self.queue = tmp_path / "q.jsonl"
        self.queue.write_text(
            json.dumps({"lane": self.lane, "repo": "acme", "task": "t"}), encoding="utf-8"
        )
        self.status_file = self.out / "lanes" / f"{self.lane}.status"
        self.gate_argvs: list[list[str]] = []
        self.gate_reached: list[int] = []
        self._pid = 900
        self._install_gate(monkeypatch)

    def _install_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A real gate that approves, writing only where it is told to.

        Approving is the gate's own decision; *where* it writes is determined
        entirely by the ``--status-file`` the dispatcher handed it.
        """

        def _approving_gate(args: object) -> int:
            self.gate_reached.append(int(getattr(args, "pr", 0) or 0))
            target = getattr(args, "status_file", None)
            assert target, "the gate was given no status file to record a verdict in"
            append_status(Path(str(target)), approved_line("abc1234def5678"))
            return 0

        monkeypatch.setattr(cli, "cmd_gate", _approving_gate)

    def _spawn(self, argv: Sequence[str], **kwargs: Any) -> Any:  # noqa: ANN401
        argv = list(argv)
        self._pid += 1
        if argv[:3] == ["fleet", "lane", "run"]:
            lane_status = Path(argv[argv.index("--status-file") + 1])
            lane_status.parent.mkdir(parents=True, exist_ok=True)
            # Exactly what runner.py writes when the gate is disabled.
            append_status(
                lane_status,
                escalation_line(
                    f"PR #{self.pr} guaranteed; gate disabled (--no-gate); "
                    f"an external gate reviews it"
                ),
            )
            Path(str(kwargs["stdout"])).write_text(
                json.dumps({"state": "pr_guaranteed", "pr": self.pr, "worktree": "/w"}),
                encoding="utf-8",
            )
            return _FakeProc(self._pid, 0, polls=1)

        self.gate_argvs.append(argv)
        gate_argv = [a for a in argv if not a.startswith("--")]
        if "--status-file" in argv:
            gate_argv += ["--status-file", argv[argv.index("--status-file") + 1]]
        try:
            code = int(cli.main(gate_argv))
        except SystemExit as exc:
            code = int(exc.code or 0)
        return _FakeProc(self._pid, code)

    def run(self) -> Any:  # noqa: ANN401
        return run_dispatch(
            operator="documents-0e",
            queue_path=self.queue,
            repos={"acme": str(self.repo)},
            max_lanes=2,
            max_gates=2,
            gate_cmd=None,
            spawn=self._spawn,
            psi_reader=lambda: IDLE,
            sleep=lambda _s: None,
            run_dir=self.out,
        )


def test_an_approving_gate_is_not_reported_as_an_escalation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate approved; the operator must see an approval.

    ``_reap_gate`` classifies the gate from the lane's status file. The
    ``--no-gate`` lane already wrote a ``NEEDS-ESCALATION`` line there, so unless
    the gate is given that file to append to, the dispatcher reads the stale
    rejection back and reports a green gate as escalated.

    The gate here is the *real* ``agent-fleet gate``, so this test also covers
    the separate defect where the gate's command line is rejected by argparse
    before it can review anything.
    """
    loop = _Loop(tmp_path, monkeypatch)
    summary = loop.run()

    assert loop.gate_reached == [loop.pr], (
        f"the gate never reviewed PR #{loop.pr} (reached={loop.gate_reached}, "
        f"argv={loop.gate_argvs})"
    )
    assert summary.approved == 1, (
        f"the gate approved but the run reported {summary.to_dict()}; the "
        f"PREMERGE-APPROVED line was never written to, or read from, "
        f"{loop.status_file}"
    )
    assert summary.escalated == 0, (
        f"an approving gate was reported as escalated: {summary.to_dict()} "
        f"(status file: {loop.status_file.read_text()!r})"
    )
    assert summary.exit_code() == 0, f"summary: {summary.to_dict()}"


def test_the_gate_is_handed_the_status_file_it_must_append_its_verdict_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate argv must name the status file the dispatcher reads back.

    The gate is the only thing that can turn the ``--no-gate`` lane's
    ``NEEDS-ESCALATION`` line into an approval. It can only do that if it is
    told where the file is.
    """
    loop = _Loop(tmp_path, monkeypatch)
    loop.run()

    assert loop.gate_argvs, "no gate was launched"
    for argv in loop.gate_argvs:
        assert "--status-file" in argv, (
            f"gate argv {argv} has no --status-file, so the gate cannot record "
            f"its verdict where _reap_gate will read it"
        )


def test_the_stale_no_gate_line_alone_classifies_as_an_escalation() -> None:
    """Control: the misread is exactly the stale ``--no-gate`` line.

    This is the line ``runner.py`` writes when the gate is disabled, fed
    straight to ``classify_status`` with a green exit code. It is the last line
    in the file, so it is the verdict -- which is why a gate that never writes
    to that file is indistinguishable from one that escalated.
    """
    stale = "12:00:00 NEEDS-ESCALATION PR #42 guaranteed; gate disabled (--no-gate)..."

    assert classify_status(stale, exit_code=0) == "escalated"
    assert "PREMERGE-APPROVED" not in stale


def test_a_verdict_appended_after_the_stale_line_wins() -> None:
    """Control: appending a verdict is what fixes it.

    Once the gate appends ``PREMERGE-APPROVED`` to the same file, the last line
    is the approval and the stale escalation is no longer the verdict. So the
    status file is the correct channel; the dispatcher simply never opens it.
    """
    stale = "12:00:00 NEEDS-ESCALATION PR #42 guaranteed; gate disabled (--no-gate)..."
    both = stale + "\n12:05:00 PREMERGE-APPROVED abc1234def"

    assert classify_status(both, exit_code=0) == "approved"
