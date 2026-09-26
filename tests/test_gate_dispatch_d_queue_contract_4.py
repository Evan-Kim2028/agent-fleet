"""contract_4: the ``gated`` counter in the JSON summary must count gates.

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
