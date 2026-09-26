"""Claim contract-2: ``GATE-SKIPPED`` hides a lane's real verdict from legacy consumers.

The status-file contract has exactly two terminal tokens, and
``docs/FLEET-OPS.md`` promises that a third line type is purely *additive*:

    ``GATE-SKIPPED`` is a third, *additional* line type: a consumer that only
    looks for the first two sees exactly what it saw before.

That promise is the one ``GATE-SKIPPED`` breaks. Before this change a skipped
gate still ended in a ``NEEDS-ESCALATION`` line, so a consumer filtering on the
two legacy tokens returned *this* run's verdict. Now the terminal line carries a
token the filter does not match, and ``last_status_line`` falls through to
whatever the lane wrote on an *earlier* run — a stale escalation that the lane
has already moved past. A monitor or dashboard driving ``lanes status`` through
that filter shows a finished, guaranteed lane as still owing a human.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet.fleet_ops.config import FleetOpsConfig, OperatorSpec
from agent_fleet.fleet_ops.runner import LaneRunResult, run_lane
from agent_fleet.fleet_ops.statusfile import (
    APPROVED_TOKEN,
    ESCALATION_TOKEN,
    GATE_SKIPPED_TOKEN,
    gate_skipped_line,
    last_status_line,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

HEAD_SHA = "a" * 40

#: The filter every existing consumer uses. These two tokens *are* the contract;
#: they predate GATE-SKIPPED and cannot have been updated to know about it.
LEGACY_TOKENS = (APPROVED_TOKEN, ESCALATION_TOKEN)

#: What an earlier run of this same lane left behind.
STALE_ESCALATION = "12:00:00 NEEDS-ESCALATION old_reason"

STREAM = "\n".join(
    [
        json.dumps({"type": "tool_completed", "subtype": "completed"}),
        json.dumps({"type": "result", "finalText": "opened the PR"}),
    ]
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "lake-of-rage"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "l@example.com")
    _git(root, "config", "user.name", "L")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "base")

    origin = tmp_path / "origin.git"
    origin.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        capture_output=True,
        text=True,
        check=True,
    )
    _git(root, "remote", "add", "origin", str(origin))
    _git(root, "push", "-u", "origin", "main")
    return root


@pytest.fixture
def task_file(tmp_path: Path) -> Path:
    path = tmp_path / "task.md"
    path.write_text("# Move the movers\n\nDo the work.\n", encoding="utf-8")
    return path


def _lane_runner(
    *, existing_pr: int | None = 3544
) -> Callable[..., subprocess.CompletedProcess[str]]:
    created: dict[str, int | None] = {"number": existing_pr}

    def runner(args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        argv = list(args)
        if argv[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(
                argv, 0, "git@github.com:Evan-Kim2028/lake-of-rage.git\n", ""
            )
        if argv[:1] == ["gh"]:
            if argv[:3] == ["gh", "pr", "list"]:
                number = created["number"]
                payload = (
                    [{"number": number, "headRefName": "fb/movers", "headRefOid": HEAD_SHA}]
                    if number
                    else []
                )
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
            if argv[:3] == ["gh", "pr", "create"]:
                created["number"] = created["number"] or 3544
                return subprocess.CompletedProcess(
                    argv, 0, f"https://github.com/o/r/pull/{created['number']}\n", ""
                )
        if any(str(a).endswith("cmd") for a in argv[:3]) or "--max-turns" in argv:
            return subprocess.CompletedProcess(argv, 0, STREAM, "")
        return subprocess.run(argv, **kwargs)

    return runner


def _with_work(repo: Path) -> None:
    _git(repo, "checkout", "-b", "fb/movers")
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")


def _run(repo: Path, task: Path, tmp_path: Path, status: Path, **kwargs: Any) -> LaneRunResult:  # noqa: ANN401
    return run_lane(
        operator="documents-1d",
        lane="movers",
        repo_path=repo,
        task_file=task,
        engine="cmd",
        config=FleetOpsConfig(operators={"documents-1d": OperatorSpec(name="documents-1d")}),
        status_file=status,
        run_dir=tmp_path / "runs",
        worktree_parent=tmp_path / "wt",
        known_gate_subcommands={"run"},
        runner=_lane_runner(),
        **kwargs,
    )


# ------------------------------------------------------------------ the defect


def test_a_legacy_consumer_does_not_see_a_stale_escalation(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    """The terminal line must be visible to the filter that predates it.

    This is the documented contract, word for word: a consumer that only looks
    for ``PREMERGE-APPROVED`` and ``NEEDS-ESCALATION`` must see what it saw
    before. Returning the previous run's escalation is not "seeing what it saw
    before" — it is seeing a verdict the lane superseded.
    """
    _with_work(repo)
    status = tmp_path / "lane.status"
    # An earlier run of this lane left a NEEDS-ESCALATION line behind.
    status.write_text(STALE_ESCALATION + "\n", encoding="utf-8")

    result = _run(repo, task_file, tmp_path, status, gate=False)

    # The lane really did reach a terminal, non-escalating outcome this run.
    assert GATE_SKIPPED_TOKEN in result.status_line
    assert ESCALATION_TOKEN not in result.status_line
    assert result.pr == 3544

    verdict = last_status_line(status, tokens=LEGACY_TOKENS)

    assert verdict != STALE_ESCALATION, (
        "a legacy two-token consumer reported a finished, guaranteed lane as still "
        f"needing escalation; last_status_line returned the stale {verdict!r}"
    )


def test_a_gate_skipped_lane_is_not_reported_as_awaiting_escalation(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    """A fresh status file, so the stale-line excuse is unavailable.

    Even with nothing prior in the file the filter comes back empty: the lane's
    real terminal verdict is simply not one the two legacy tokens can see. A
    consumer that cannot distinguish "finished, waiting on the gate" from "no
    verdict at all" is what the additive claim rules out.
    """
    _with_work(repo)
    status = tmp_path / "lane.status"

    result = _run(repo, task_file, tmp_path, status, gate=False)

    assert GATE_SKIPPED_TOKEN in result.status_line
    verdict = last_status_line(status, tokens=LEGACY_TOKENS)

    # The unfiltered read is the lane's real, current terminal line ...
    assert last_status_line(status) == result.status_line
    # ... so a consumer filtering on the two legacy tokens must be able to see it
    # too, and must not come back empty for a lane that plainly finished.
    assert verdict == result.status_line, (
        "a legacy two-token consumer cannot see a completed lane's terminal "
        f"verdict; got {verdict!r} for the lane's own line {result.status_line!r}"
    )


def test_the_gate_skipped_line_is_additive_to_the_legacy_filter(tmp_path: Path) -> None:
    """Pin the mechanism: the new token is invisible to the pre-existing filter.

    On origin/main the same ``--no-gate`` lane ended in a ``NEEDS-ESCALATION``
    line, so the filter returned the lane's real terminal verdict. The new token
    is what breaks that. "Additive" would mean a legacy consumer's view is
    unchanged — here it silently changes from *this run's verdict* to the
    *previous run's* verdict, or to nothing at all.
    """
    status = tmp_path / "lane.status"
    skipped = gate_skipped_line(3544, sha=HEAD_SHA, reason="gate disabled (--no-gate)")
    status.write_text(f"{STALE_ESCALATION}\n{skipped}\n", encoding="utf-8")

    # The unfiltered read is this run's terminal line ...
    assert last_status_line(status) == skipped
    # ... and a legacy consumer must be able to see it too, rather than being
    # handed the escalation this lane already moved past.
    assert last_status_line(status, tokens=LEGACY_TOKENS) == skipped, (
        "adding a third token silently changed what an existing two-token "
        "consumer reports; it is now reading a superseded verdict"
    )
