"""contract_4: what the dispatcher counts, and whose verdict it counts.

Claim under test
----------------
``DispatchSummary.gated`` is declared as a field and emitted by ``to_dict()``,
so it is part of the documented ``--json`` output of ``fleet dispatch``. Nothing
increments it: the only two occurrences in ``dispatch.py`` are the field
declaration and the ``to_dict()`` entry, and no ``_apply`` / ``_launch_gate`` /
``_reap_gate`` / ``_tally`` path touches it. So the counter is permanently 0,
even for a run that gated PRs.

The test runs a dispatch that really does launch a gate and asserts the counter
matches the gates launched -- not that it is merely non-zero, so a hardcoded
value cannot pass. A cooperative gate stand-in writes its verdict to the lane's
status file, so the run is a complete, ordinary, all-approved one; the only
thing under test is the counter.

The same file also pins what a *counted* approval has to mean. ``_status_for``
classifies the last line of ``lanes/<lane>.status``, and that path is keyed by
lane name, which is reused: a re-dispatch whose durable state was lost resolves
to the same file, so a ``PREMERGE-APPROVED`` line from an earlier PR can still
be sitting there. Unless the dispatcher empties the file before spawning the
gate, a gate that crashes writes nothing and that stale line is read back as its
verdict -- an unreviewed PR reported approved, with exit 0.
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
GATED_LANES = 2


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


def test_the_summary_counts_every_gate_it_launched(tmp_path: Path) -> None:
    """``gated`` must equal the number of gates spawned, per-lane and in total."""
    gates_launched, summary = _run_two_gated_lanes(tmp_path)

    assert len(gates_launched) == GATED_LANES, "sanity: two gates really ran"

    gated_lanes = sorted(
        name for name, lane in summary.state.lanes.items() if lane.gate_pid is not None
    )
    assert len(gated_lanes) == GATED_LANES, f"sanity: two lanes hold gate identities: {gated_lanes}"

    assert summary.to_dict()["gated"] == GATED_LANES, (
        f"to_dict() reported gated={summary.to_dict()['gated']} for a run that "
        f"launched {len(gates_launched)} gates; the counter is never incremented"
    )


def test_the_gated_counter_appears_in_the_json_document(tmp_path: Path) -> None:
    """The key is part of the documented ``--json`` output, so it has to be right.

    Serialising the summary is what an operator (or a wrapper script) reads; a
    field that is always zero is worse than a missing one, because it looks
    like a measurement.
    """
    _gates, summary = _run_two_gated_lanes(tmp_path)

    payload = json.loads(json.dumps(summary.to_dict()))

    assert "gated" in payload, f"the --json payload has no 'gated' key: {sorted(payload)}"
    assert payload["gated"] == GATED_LANES, (
        f"--json reported gated={payload['gated']} after gating {GATED_LANES} PRs"
    )
    # Sanity: the rest of the summary is populated, so this is not a run that
    # failed to gate anything.
    assert payload["approved"] == GATED_LANES, f"summary: {payload}"


def _run_one_gated_lane(
    tmp_path: Path, *, gate_exit: int, stale_status: str | None, operator: str
) -> tuple[Path, Any]:
    """Run a one-lane queue whose gate either writes a verdict or writes nothing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    lane = "L1"
    queue = tmp_path / "q.jsonl"
    queue.write_text(json.dumps({"lane": lane, "repo": "acme", "task": "t"}), encoding="utf-8")
    out = tmp_path / "out"
    status = out / "lanes" / f"{lane}.status"
    if stale_status is not None:
        status.parent.mkdir(parents=True, exist_ok=True)
        status.write_text(stale_status, encoding="utf-8")
    _pid = 3000

    def spawn(argv: Sequence[str], **kwargs: Any) -> Any:  # noqa: ANN401
        nonlocal _pid
        argv = list(argv)
        _pid += 1
        if argv[:3] == ["fleet", "lane", "run"]:
            Path(str(kwargs["stdout"])).write_text(
                json.dumps({"state": "pr_guaranteed", "pr": 7, "worktree": "/w"}),
                encoding="utf-8",
            )
            return _FakeProc(_pid, 0, polls=1)

        if gate_exit == 0:
            Path(argv[argv.index("--status-file") + 1]).write_text(
                "12:05:00 PREMERGE-APPROVED bbb222fff\n", encoding="utf-8"
            )
        return _FakeProc(_pid, gate_exit)

    summary = run_dispatch(
        operator=operator,
        queue_path=queue,
        repos={"acme": str(repo)},
        max_lanes=2,
        max_gates=2,
        spawn=spawn,
        psi_reader=lambda: IDLE,
        sleep=lambda _s: None,
        run_dir=out,
    )
    return status, summary


def test_a_gate_that_wrote_nothing_does_not_inherit_an_earlier_runs_approval(
    tmp_path: Path,
) -> None:
    """Lane names are reused, so a stale approval must not stand in for a verdict.

    ``_status_for`` classifies the last line of ``lanes/<lane>.status``. A
    re-dispatch whose durable state was lost resolves to that same path, so a
    ``PREMERGE-APPROVED`` line from an earlier PR is still there. If the status
    file is not reset before the gate runs, a gate that crashes writes nothing
    and that stale line is read back as its verdict — an unreviewed PR reported
    approved, with exit 0.
    """
    status, summary = _run_one_gated_lane(
        tmp_path,
        gate_exit=1,
        stale_status="12:00:00 PREMERGE-APPROVED oldrun111\n",
        operator="documents-0e-stale",
    )

    record = summary.state.lanes["L1"]
    assert not summary.approved, (
        f"a gate that exited {1} and wrote no verdict was reported as approved from "
        f"the stale line in {status}: {summary.to_dict()}"
    )
    assert summary.escalated == 1, f"summary: {summary.to_dict()}"
    assert summary.exit_code() == 1, f"summary: {summary.to_dict()}"
    assert record.reason is not None and record.reason.startswith("escalated"), (
        f"lane L1 was finished as {record.state}/{record.reason!r} instead of escalated"
    )


def test_a_gate_verdict_is_still_approved_by_the_same_run(tmp_path: Path) -> None:
    """Control: emptying the file costs a real approval nothing.

    The reset above is only safe because the gate appends its own verdict to the
    same path. A reset that also swallowed that verdict would turn every green
    gate into an escalation, so the run is driven through the same helper with
    the stale line removed and has to come back approved.
    """
    _status, summary = _run_one_gated_lane(
        tmp_path, gate_exit=0, stale_status=None, operator="documents-0e-control"
    )

    assert summary.approved == 1, f"summary: {summary.to_dict()}"
    assert summary.escalated == 0, f"summary: {summary.to_dict()}"
    assert summary.exit_code() == 0, f"summary: {summary.to_dict()}"


def test_the_status_file_is_empty_before_the_gate_is_spawned(tmp_path: Path) -> None:
    """The reset has to happen at launch, not after the gate exits.

    Reading the file the moment the gate is spawned is the only point at which
    the reset is observable from outside: by the time the gate exits it has
    appended its own verdict. So the gate is told to append nothing and the file
    is read at the moment of the spawn.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    queue = tmp_path / "q.jsonl"
    queue.write_text(json.dumps({"lane": "L1", "repo": "acme", "task": "t"}), encoding="utf-8")
    out = tmp_path / "out"
    at_spawn: list[str] = []
    _pid = 4000

    def spawn(argv: Sequence[str], **kwargs: Any) -> Any:  # noqa: ANN401
        nonlocal _pid
        argv = list(argv)
        _pid += 1
        if argv[:3] == ["fleet", "lane", "run"]:
            lane_status = Path(argv[argv.index("--status-file") + 1])
            lane_status.parent.mkdir(parents=True, exist_ok=True)
            lane_status.write_text("12:00:00 NEEDS-ESCALATION gate disabled (--no-gate)\n")
            Path(str(kwargs["stdout"])).write_text(
                json.dumps({"state": "pr_guaranteed", "pr": 7, "worktree": "/w"}),
                encoding="utf-8",
            )
            return _FakeProc(_pid, 0, polls=1)

        at_spawn.append(Path(argv[argv.index("--status-file") + 1]).read_text(encoding="utf-8"))
        Path(argv[argv.index("--status-file") + 1]).write_text(
            "12:05:00 PREMERGE-APPROVED bbb222fff\n", encoding="utf-8"
        )
        return _FakeProc(_pid, 0)

    summary = run_dispatch(
        operator="documents-0e-reset",
        queue_path=queue,
        repos={"acme": str(repo)},
        max_lanes=2,
        max_gates=2,
        spawn=spawn,
        psi_reader=lambda: IDLE,
        sleep=lambda _s: None,
        run_dir=out,
    )

    assert at_spawn == [""], (
        f"the lane's status file still held the --no-gate escalation line when the "
        f"gate was spawned ({at_spawn!r}); the gate appends to whatever it finds, so "
        "the dispatcher's own stale text would be classified as the gate's verdict"
    )
    assert summary.approved == 1, f"summary: {summary.to_dict()}"


def _run_two_gated_lanes(tmp_path: Path) -> tuple[list[str], Any]:
    """Run a two-lane queue where both lanes produce a PR and both get gated."""
    repo = tmp_path / "repo"
    repo.mkdir()
    lanes = [f"lane-{n}" for n in range(GATED_LANES)]
    queue = tmp_path / "q.jsonl"
    queue.write_text(
        "\n".join(
            json.dumps({"lane": lane, "repo": "acme", "task": f"task {lane}"}) for lane in lanes
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out"
    gates_launched: list[str] = []
    _pid = 7000

    def spawn(argv: Sequence[str], **kwargs: Any) -> Any:  # noqa: ANN401
        nonlocal _pid
        argv = list(argv)
        _pid += 1
        if argv[:3] == ["fleet", "lane", "run"]:
            Path(str(kwargs["stdout"])).write_text(
                json.dumps(
                    {
                        "state": "pr_guaranteed",
                        "pr": 100 + len(gates_launched),
                        "worktree": "/w",
                    }
                ),
                encoding="utf-8",
            )
            return _FakeProc(_pid, 0, polls=1)

        gates_launched.append(argv[argv.index("--lane") + 1])
        # A gate that records its verdict where the dispatcher reads it back.
        status = out / "lanes" / f"{gates_launched[-1]}.status"
        status.parent.mkdir(parents=True, exist_ok=True)
        status.write_text("12:00:00 PREMERGE-APPROVED abc1234def\n", encoding="utf-8")
        return _FakeProc(_pid, 0)

    summary = run_dispatch(
        operator="documents-0e",
        queue_path=queue,
        repos={"acme": str(repo)},
        max_lanes=GATED_LANES,
        max_gates=GATED_LANES,
        spawn=spawn,
        psi_reader=lambda: IDLE,
        sleep=lambda _s: None,
        run_dir=out,
    )
    return gates_launched, summary
