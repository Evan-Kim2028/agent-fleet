"""prodsafety_1: confining an untrusted name must not merge two distinct names.

Claim under test
----------------
``confined_name`` folds an untrusted ``lane`` (or ``--operator``) into one path
component, and every per-lane and per-operator artifact is keyed by it:

* ``task_file_for`` / ``status_file_for`` -- the task file the lane is handed and
  the one file the gate appends its verdict to and ``_reap_gate`` reads back.
* ``dispatch_state_path`` -- the durable state a dispatcher reloads on restart.
* the lane and gate log paths.

Folding is lossy.  ``docs/alpha`` and ``docs-alpha`` both fold to ``docs-alpha``,
so two lanes in one queue resolve to the *same* status file.  Both gates are
handed that one path, so whichever finishes last stamps its verdict over the
other's, and the reaper classifies both lanes from it.  With one gate approving
and the other escalating, the escalated PR is reported ``approved`` and the
dispatcher exits 0 -- an unreviewed PR auto-merged.  The same collision lets one
operator reload another operator's terminal ``state.json``.

The fix makes the name a function of the whole value: the folded form is kept
for readability, and a digest of the *original* value makes it injective.  The
digest is what these tests pin, since readable prefixes are allowed to repeat.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet.fleet_ops import pressure
from agent_fleet.fleet_ops.dispatch import (
    DispatchItem,
    DispatchLane,
    confined_name,
    dispatch_state_path,
    load_state,
    run_dispatch,
    save_state,
    status_file_for,
    task_file_for,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

IDLE = pressure.Throttle(some_avg10=0.0, path=Path("/fake/cpu.pressure"), available=True)

#: Two lanes that fold identically but are different lanes in the queue.
LANE_SLASH = "docs/alpha"
LANE_DASH = "docs-alpha"


class _Proc:
    """A child that exits after *polls* polls, so a lane works then finishes."""

    def __init__(self, pid: int, exit_code: int | None = None, *, polls: int = 0) -> None:
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
    home = tmp_path / "afhome"
    home.mkdir()
    monkeypatch.setenv("AGENT_FLEET_HOME", str(home))


def test_two_lanes_that_fold_alike_get_different_status_files(tmp_path: Path) -> None:
    """The collision itself: one status file per lane, not per folded name."""
    slash = status_file_for(tmp_path, LANE_SLASH)
    dash = status_file_for(tmp_path, LANE_DASH)

    assert slash != dash, (
        f"{LANE_SLASH!r} and {LANE_DASH!r} both resolve to {slash}; the gate writes "
        "its verdict there and the reaper reads it back, so the two lanes share "
        "one verdict"
    )
    assert task_file_for(tmp_path, LANE_SLASH) != task_file_for(tmp_path, LANE_DASH)
    assert len({confined_name(LANE_SLASH), confined_name(LANE_DASH)}) == 2


def test_the_folded_prefix_stays_readable() -> None:
    """The digest is a suffix, not a replacement: operators still see the lane."""
    for value in (LANE_SLASH, "alpha", "gate-standard-tier"):
        token = confined_name(value)
        folded = "".join(c if c.isalnum() or c in "._-" else "-" for c in value).strip("-.")
        assert token.startswith(folded), f"{value!r} -> {token!r} lost its readable prefix"


def test_a_name_is_still_exactly_one_path_component(tmp_path: Path) -> None:
    """Adding a digest must not weaken the traversal guarantee."""
    for hostile in ("../../ESCAPED", "/abs/operator", "..", "a/b/c", "."):
        token = confined_name(hostile)
        assert "/" not in token, f"{hostile!r} -> {token!r} is not one component"
        assert token not in {".", ".."}
        assert not token.startswith("."), f"{hostile!r} -> {token!r} is a dotfile"
        assert (tmp_path / "lanes" / f"{token}.status").parent == tmp_path / "lanes"

    # A value that folds away entirely is still named, not written as "unnamed".
    assert confined_name("/") == confined_name("/")
    assert confined_name("/") not in {".", "..", "unnamed"}


def test_two_operators_that_fold_alike_get_different_state_files() -> None:
    """One operator must never reload another's durable state."""
    slash, dash = "ops/alpha", "ops-alpha"
    assert dispatch_state_path(slash) != dispatch_state_path(dash)

    first = load_state(slash)
    first.lanes[LANE_SLASH] = DispatchLane(
        lane=LANE_SLASH,
        item=DispatchItem(lane=LANE_SLASH, repo="acme", task="t"),
        state="done",
        reason="approved",
    )
    save_state(first)

    # The colliding operator reloads someone else's record, under their operator.
    inherited = load_state(dash)
    assert inherited.operator == dash, (
        f"load_state({dash!r}) read the state saved by {slash!r}: "
        f"operator came back as {inherited.operator!r}"
    )
    assert load_state(slash).operator == slash


@pytest.mark.parametrize("approver", [LANE_SLASH, LANE_DASH])
def test_each_lane_is_classified_from_its_own_gate(tmp_path: Path, approver: str) -> None:
    """End to end: only the lane whose gate approved may be reported approved.

    The repro the report describes.  Two lanes that share a status file, gated
    with opposite verdicts: the file is stamped twice and the second writer
    wins, so before the fix the pair ends up *both* approved or *both* escalated
    depending on which gate finished last -- including the dangerous direction,
    an escalated PR reported approved with exit 0.  ``approver`` is
    parametrized over both lanes so the test pins both directions rather than
    whichever one the scheduler happened to run last: after the fix each lane is
    classified from its own gate's verdict, which is the whole contract of
    ``_status_for``.
    """
    queue = tmp_path / "q.jsonl"
    queue.write_text(
        "\n".join(
            json.dumps({"lane": lane, "repo": "acme", "task": "t", "pr": pr})
            for lane, pr in ((LANE_DASH, 11), (LANE_SLASH, 12))
        ),
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    out = tmp_path / "out"

    verdicts = {
        lane: (
            "PREMERGE-APPROVED abc1234def"
            if lane == approver
            else "NEEDS-ESCALATION: needs a human"
        )
        for lane in (LANE_SLASH, LANE_DASH)
    }
    stage = {"n": 0}

    def _spawn(argv: Sequence[str], **kwargs: Any) -> Any:  # noqa: ANN401
        stage["n"] += 1
        lane = argv[argv.index("--lane") + 1]
        is_lane = "gate" not in argv
        if is_lane:
            with kwargs["stdout"] as log:
                log.write(json.dumps({"state": "pr_guaranteed", "pr": 12, "worktree": "/w"}))
        else:
            # The gate is told its exact status path; write there, as a real one does.
            status = Path(argv[argv.index("--status-file") + 1])
            status.parent.mkdir(parents=True, exist_ok=True)
            status.write_text(f"12:00:00 {verdicts[lane]}\n", encoding="utf-8")
        return _Proc(1000 + stage["n"], 0, polls=1 if is_lane else 0)

    summary = run_dispatch(
        operator="documents-0e",
        queue_path=queue,
        repos={"acme": str(repo)},
        max_lanes=4,
        max_gates=4,
        spawn=_spawn,
        psi_reader=lambda: IDLE,
        sleep=lambda _s: None,
        run_dir=out,
    )

    lanes = summary.state.lanes
    expected = {approver: "approved", _other(approver): "escalated"}
    for lane, reason in expected.items():
        assert lanes[lane].reason == reason, (
            f"lane {lane!r} was reaped {lanes[lane].reason!r} from status file "
            f"{lanes[lane].status_file}, but its own gate wrote {verdicts[lane]!r} "
            f"and the other lane's gate wrote {verdicts[_other(lane)]!r}. Both lanes "
            "fold to one status file, so one lane's verdict was read as the other's."
        )
    assert summary.approved == 1, "exactly the approving lane may be counted approved"
    assert summary.escalated == 1
    assert summary.errors == 0


def _other(lane: str) -> str:
    return LANE_DASH if lane == LANE_SLASH else LANE_SLASH
