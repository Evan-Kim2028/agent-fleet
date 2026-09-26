"""Defect 2: a lane that produced no changes must say *why*, not ``commit_failed``.

When the engine exits and the worktree is clean with no commits ahead of base,
the guarantee has nothing to publish. The lane used to escalate with
``no_commits_ahead`` and a boilerplate string, which is indistinguishable from
a broken lane. Two very different things produce that state:

* the implementer **stopped on purpose** — a fence, an owner decision, a need
  for clarification — and said so. That is not a failure; it is a decision an
  orchestrator should route to a human. The reason is in the engine's final
  text and was being thrown away.
* the implementer **stopped mid-intention** — a short final text that reads like
  "Let me verify…" or "Now I'll…" and never finished. That is the lazy exit
  fbrun already knew how to spot, and it is worth exactly one automatic retry
  with a nudge before being called a stop.

The classification is a decision, not a formatting nicety: it decides whether a
lane gets retried, routed to a human, or reported as a broken manager.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet.fleet_ops import lazyexit
from agent_fleet.fleet_ops.registry import STATE_PR_GUARANTEED
from agent_fleet.fleet_ops.runner import LaneRunResult, run_lane

if TYPE_CHECKING:
    from collections.abc import Callable

HEAD_SHA = "a" * 40

#: A real work stream: tool calls, then a result.
STREAM = "\n".join(
    [
        json.dumps({"type": "tool_completed", "subtype": "completed"}),
        json.dumps({"type": "result", "finalText": "opened the PR"}),
    ]
)

#: A *short* stream whose final text is a stated stop, not an intention. This is
#: the case that has to be routed to a human rather than retried forever.
STOPPED_STREAM = "\n".join(
    [
        json.dumps({"type": "tool_completed", "subtype": "completed"}),
        json.dumps({"type": "tool_completed", "subtype": "completed"}),
        json.dumps({"type": "tool_completed", "subtype": "completed"}),
        json.dumps({"type": "tool_completed", "subtype": "completed"}),
        json.dumps({"type": "tool_completed", "subtype": "completed"}),
        json.dumps(
            {
                "type": "result",
                "finalText": (
                    "I am stopping here and not changing any files. The task asks me to "
                    "edit the lake print_identity rule, and that file is under an explicit "
                    "owner fence; editing it would reverse a decision another session made. "
                    "Routing this to the owner for a decision instead."
                ),
            }
        ),
    ]
)

#: The lazy shape: short, and phrased as work about to happen.
LAZY_STREAM = "\n".join(
    [
        json.dumps({"type": "tool_completed", "subtype": "completed"}),
        json.dumps({"type": "tool_completed", "subtype": "completed"}),
        json.dumps({"type": "result", "finalText": "Now I'll update the manifest:"}),
    ]
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):  # noqa: ANN001, ANN202
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
    """A runner handing out *streams* one per engine invocation, in order.

    Returns the runner and a live ``{"engine": n}`` counter, which is what makes
    the retry observable: a lane that retries runs the engine a second time, and
    the second stream is the one that decides whether the retry was worth it.
    """
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
        # The fake engine is the default: these tests are about what the runner
        # does with a final text, and a real `cmd` would not produce one.
        "runner": _lane_runner(streams)[0],
    }
    return run_lane(**{**opts, **kwargs})


# --------------------------------------------------- the final-text classifier


def test_a_short_unfinished_intention_reads_as_a_lazy_exit() -> None:
    assert lazyexit.looks_like_unfinished_intention("Now I'll update the manifest:") is True


def test_a_stated_stop_is_not_an_unfinished_intention() -> None:
    text = (
        "I am stopping here and not changing any files. The task asks me to edit a fenced "
        "file, and that file is under an explicit owner fence; routing this to the owner."
    )
    assert lazyexit.looks_like_unfinished_intention(text) is False


def test_a_long_finished_answer_is_not_an_unfinished_intention() -> None:
    """Length alone must never be the signal — a real report is long and complete."""
    text = "I investigated the failure and fixed the parser. " + "Details follow. " * 40
    assert lazyexit.looks_like_unfinished_intention(text) is False


def test_a_completion_claim_is_not_an_unfinished_intention() -> None:
    """ "opened the PR" is first person and short, but it is not a plan.

    This is the false positive that made the classification dangerous: an engine
    narrates a completion it did not achieve far more often than it stops
    mid-sentence, and a needless retry costs a full engine run each time.
    """
    assert lazyexit.looks_like_unfinished_intention("opened the PR") is False
    assert lazyexit.looks_like_unfinished_intention("Done - committed and pushed.") is False


def test_an_ambiguous_mix_resolves_toward_not_retrying() -> None:
    """ "Let me verify the tests pass" could be read either way, so it is not retried.

    The tie-break is deliberate and one-directional: ambiguity resolves to
    *not* retrying, because the two errors are not symmetric. A needless retry
    burns a full engine run to learn nothing; a missed retry is one escalation
    carrying the implementer's own final text, which is what an operator reads
    anyway.
    """
    assert lazyexit.looks_like_unfinished_intention("Let me verify the tests pass") is False


def test_an_empty_final_text_is_not_an_unfinished_intention() -> None:
    """Nothing to judge is a ``no_changes_stopped`` with no reason, not a retry."""
    assert lazyexit.looks_like_unfinished_intention("") is False
    assert lazyexit.looks_like_unfinished_intention("NO RESULT EVENT") is False


# ------------------------------------------------------------ no_changes_stopped


def test_a_stated_stop_is_reported_as_no_changes_stopped(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    result = _run(repo, task_file, tmp_path, [STOPPED_STREAM])

    assert result.escalated
    assert result.reason == "no_changes_stopped"
    assert "owner fence" in result.detail


def test_the_implementers_reason_reaches_the_status_line(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    """The orchestrator reads the status file, so the reason has to be in it."""
    result = _run(repo, task_file, tmp_path, [STOPPED_STREAM])

    assert "NEEDS-ESCALATION" in result.status_line
    assert "no_changes_stopped" in result.status_line
    assert "owner fence" in result.status_line
    # One line, whatever the implementer wrote.
    assert "\n" not in result.status_line


def test_a_stated_stop_is_not_retried(repo: Path, task_file: Path, tmp_path: Path) -> None:
    """Retrying a deliberate stop burns a full engine run to get the same answer."""
    runner, seen = _lane_runner([STOPPED_STREAM])
    _run(repo, task_file, tmp_path, [STOPPED_STREAM], runner=runner)
    assert seen["engine"] == 1


def test_a_worked_branch_is_unaffected(repo: Path, task_file: Path, tmp_path: Path) -> None:
    """The classification only applies to the no-change path."""
    base, _ = _lane_runner([STREAM])

    def work_then_answer(args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        argv = list(args)
        if "--max-turns" in argv or any(str(a).endswith("cmd") for a in argv[:3]):
            worktree = Path(str(kwargs.get("cwd") or ""))
            (worktree / "feature.py").write_text("x = 1\n", encoding="utf-8")
        return base(args, **kwargs)

    result = _run(repo, task_file, tmp_path, [STREAM], runner=work_then_answer)

    assert result.pr == 3544
    assert result.state == STATE_PR_GUARANTEED
    assert result.reason != "no_changes_stopped"


# -------------------------------------------------------------------- lazy_exit


def test_a_lazy_exit_is_retried_once_with_a_nudge(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    """The retry is the whole point: the first run stopped before doing anything.

    The trigger requires an untouched tree, so nothing is written until the run
    after the nudge — and then it goes into the lane's own worktree, which is
    where a real implementer's work would land.
    """
    prompts: list[str] = []
    base, _ = _lane_runner([LAZY_STREAM, STREAM])

    def do_work_on_second_run(args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        argv = list(args)
        is_engine = "--max-turns" in argv or any(str(a).endswith("cmd") for a in argv[:3])
        if is_engine:
            prompts.append(next((a for a in argv if isinstance(a, str) and " " in a), ""))
            if len(prompts) > 1:
                (Path(str(kwargs.get("cwd") or "")) / "feature.py").write_text(
                    "x = 1\n", encoding="utf-8"
                )
        return base(args, **kwargs)

    result = _run(repo, task_file, tmp_path, [], runner=do_work_on_second_run)

    # The retry prompt is the nudge, not a re-send of the original task.
    nudges = [p for p in prompts if "stopped before doing the work" in p.lower()]
    assert nudges, f"the retry must carry a nudge, got prompts={prompts}"
    assert "do not re-plan" in nudges[0].lower()
    assert result.pr == 3544


def test_a_retry_that_stays_lazy_is_reported_as_lazy_exit(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    """One retry, not a loop: a second lazy exit is a lane-level failure."""
    runner, seen = _lane_runner([LAZY_STREAM, LAZY_STREAM])

    result = _run(repo, task_file, tmp_path, [], runner=runner)

    assert seen["engine"] == 2
    assert result.escalated
    assert result.reason == "lazy_exit"


def test_the_retry_is_recorded_in_events(repo: Path, task_file: Path, tmp_path: Path) -> None:
    from agent_fleet.fleet_ops import registry

    _run(repo, task_file, tmp_path, [], runner=_lane_runner([LAZY_STREAM, LAZY_STREAM])[0])

    events = registry.read_events(operator="documents-1d", lane="movers")
    names = [e["event"] for e in events]
    assert "lane.engine.retry" in names
    retry = next(e for e in events if e["event"] == "lane.engine.retry")
    assert retry["reason"] == "lazy_exit"
    assert retry["attempt"] == 1


def test_the_second_lazy_exit_explains_itself_in_the_detail(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    result = _run(repo, task_file, tmp_path, [], runner=_lane_runner([LAZY_STREAM, LAZY_STREAM])[0])

    assert "Now I'll update the manifest:" in result.detail
    assert "NEEDS-ESCALATION" in result.status_line
    assert "lazy_exit" in result.status_line


def test_a_run_with_no_final_text_is_not_retried(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    """No final text means the stream never said anything — nothing to nudge about.

    The guarantee's own ``no_commits_ahead`` is the verdict then. Classifying it
    as ``no_changes_stopped`` would put a fabricated "reason" on the status line
    for a lane that gave no reason at all.
    """
    silent = json.dumps({"type": "tool_completed", "subtype": "completed"})
    runner, seen = _lane_runner([silent])

    result = _run(repo, task_file, tmp_path, [], runner=runner)

    assert seen["engine"] == 1
    assert result.reason == "no_commits_ahead"
    assert result.no_change_detail == ""
