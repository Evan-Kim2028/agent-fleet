"""Claim contract-1: a deliberate stop is misreported as ``lazy_exit``.

``INTENTION_PATTERNS`` matches first-person *future* phrasing anywhere in the
closing words of the final text, so an implementer that explains a genuine
decision in the first person — "I'll hold off until the owner decides" — is
read as a run that ran out of steam. The lane then spends a second engine run on
a nudge, produces nothing again, and escalates as ``lazy_exit`` instead of the
``no_changes_stopped`` it promised.

The classification decides whether a lane is retried or routed to a human, so a
false positive is not a cosmetic mislabel: it costs a full engine run and hides
the implementer's own stated reason behind a different reason token.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet.fleet_ops import lazyexit
from agent_fleet.fleet_ops.runner import LaneRunResult, run_lane

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

HEAD_SHA = "a" * 40

#: A real decision, explained in the first person. Every one of these phrasings is
#: an announcement of a *completed* choice ("I will wait for the owner"), not an
#: announcement of work the engine failed to do. The text is under
#: ``MAX_INTENTION_CHARS`` and claims no completion word, so the only thing that
#: can classify it is the first-person future phrase.
DELIBERATE_STOPS: dict[str, str] = {
    "i_ll_hold_off": (
        "The print_identity rule is under an explicit owner fence. I'll hold off until "
        "the owner decides, and I'm stopping here rather than working around the fence."
    ),
    "going_to": (
        "The print_identity rule is under an explicit owner fence, so I am going to wait "
        "for the owner instead of editing it."
    ),
    "then_i": (
        "The rule is fenced, and then I would be guessing about the owner's intent, so I "
        "left it alone."
    ),
}

STOPPED_STREAM = json.dumps({"type": "result", "finalText": DELIBERATE_STOPS["i_ll_hold_off"]})


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
    path.write_text("# Fix the thing\n\nDo the work.\n", encoding="utf-8")
    return path


def _lane_runner(
    streams: list[str], *, slug: str = "Evan-Kim2028/lake-of-rage"
) -> tuple[Callable[..., subprocess.CompletedProcess[str]], dict[str, int]]:
    """Hand out *streams*, one per engine invocation, and count the invocations."""
    created: dict[str, int | None] = {"number": None}
    seen: dict[str, int] = {"engine": 0}

    def runner(args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        argv = list(args)
        if argv[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(argv, 0, f"git@github.com:{slug}.git\n", "")
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
                created["number"] = 3544
                return subprocess.CompletedProcess(
                    argv, 0, "https://github.com/o/r/pull/3544\n", ""
                )
        if any(str(a).endswith("cmd") for a in argv[:3]) or "--max-turns" in argv:
            index = seen["engine"]
            seen["engine"] += 1
            stream = streams[index] if index < len(streams) else streams[-1]
            return subprocess.CompletedProcess(argv, 0, stream, "")
        return subprocess.run(argv, **kwargs)

    return runner, seen


def _run(
    repo: Path,
    task: Path,
    tmp_path: Path,
    streams: list[str],
    **kwargs: Any,  # noqa: ANN401
) -> LaneRunResult:
    from agent_fleet.fleet_ops.config import FleetOpsConfig, OperatorSpec

    opts: dict[str, Any] = {
        "operator": "documents-1d",
        "lane": "movers",
        "repo_path": repo,
        "task_file": task,
        "engine": "cmd",
        "config": FleetOpsConfig(operators={"documents-1d": OperatorSpec(name="documents-1d")}),
        "status_file": tmp_path / "lane.status",
        "run_dir": tmp_path / "runs",
        "worktree_parent": tmp_path / "wt",
        "known_gate_subcommands": {"run"},
        "runner": _lane_runner(streams)[0],
    }
    return run_lane(**{**opts, **kwargs})


def _stream_for(text: str) -> str:
    """A *worked* run that still ends on *text* — tool calls, then that answer."""
    return "\n".join(
        [
            json.dumps({"type": "tool_completed", "subtype": "completed"}),
            json.dumps({"type": "tool_completed", "subtype": "completed"}),
            json.dumps({"type": "tool_completed", "subtype": "completed"}),
            json.dumps({"type": "result", "finalText": text}),
        ]
    )


# --------------------------------------------------------------- the classifier


@pytest.mark.parametrize("key", sorted(DELIBERATE_STOPS))
def test_a_deliberate_stop_is_not_an_unfinished_intention(key: str) -> None:
    """The first-person future phrase is not, by itself, evidence of a mid-thought stop."""
    text = DELIBERATE_STOPS[key]
    assert lazyexit.looks_like_unfinished_intention(text) is False, (
        f"{key!r} describes a decision already taken, but reads as an unfinished intention"
    )


# ------------------------------------------------------------- the lane outcome


def test_a_deliberate_stop_is_not_retried(repo: Path, task_file: Path, tmp_path: Path) -> None:
    """A decision the implementer already made is worth one engine run, not two."""
    stream = _stream_for(DELIBERATE_STOPS["i_ll_hold_off"])
    runner, seen = _lane_runner([stream])

    _run(repo, task_file, tmp_path, [], runner=runner)

    assert seen["engine"] == 1, "a stated decision was retried, wasting a full engine run"


def test_a_deliberate_stop_reports_no_changes_stopped_not_lazy_exit(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    """The promised outcome: the implementer's own reason, on a human's list."""
    stream = _stream_for(DELIBERATE_STOPS["i_ll_hold_off"])
    runner, seen = _lane_runner([stream])

    result = _run(repo, task_file, tmp_path, [], runner=runner)

    assert result.escalated
    assert result.reason == "no_changes_stopped", (
        f"a deliberate fenced stop was misreported as {result.reason!r} "
        f"after {seen['engine']} engine runs"
    )
    assert "NEEDS-ESCALATION" in result.status_line
    assert "no_changes_stopped" in result.status_line
    # The implementer's own reason still reaches the operator.
    assert "owner fence" in result.detail
