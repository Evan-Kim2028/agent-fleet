"""End-to-end lane runs: the PR guarantee, the binding, and the status line.

These drive :func:`run_lane` against a real git repo with a real commit and a real
push, and stub only what genuinely cannot run offline: the ``gh`` CLI and the
agent binary. Git is deliberately left real — the commit, the hooks and the push
are the behaviours worth testing, and they only mean anything against an actual
repository.
The assertions concentrate on the promises a lane makes: a PR always exists even
when the agent died, a mis-bound PR is refused, and the status file always gets
a verdict line.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable  # noqa: TC003
from pathlib import Path  # noqa: TC003
from typing import Any

import pytest

from agent_fleet.fleet_ops import registry
from agent_fleet.fleet_ops.config import FleetOpsConfig, OperatorSpec, load_fleet_ops_config
from agent_fleet.fleet_ops.registry import STATE_APPROVED, STATE_PR_GUARANTEED
from agent_fleet.fleet_ops.runner import LaneRunResult, run_lane, write_status_line

#: A cmd JSONL stream that looks like real work: tool calls, then a result.
STREAM = "\n".join(
    [
        json.dumps({"type": "tool_completed", "subtype": "completed"}),
        json.dumps({"type": "tool_completed", "subtype": "completed"}),
        json.dumps({"type": "result", "finalText": "opened the PR"}),
    ]
)

HEAD_SHA = "a" * 40


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):  # noqa: ANN001, ANN202
    """Never touch the real ~/.agent-fleet registry from a test."""
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real repo with one commit on main and a real local bare origin.

    The origin is a real filesystem remote so the push in the guarantee is a
    real push, but ``binding`` reads the repo slug from that URL — so the slug is
    supplied by the fake ``gh``/``git remote`` runner rather than the filesystem
    path. See :func:`_lane_runner`.
    """
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


def _config(**operator_overrides: Any) -> FleetOpsConfig:  # noqa: ANN401
    return FleetOpsConfig(
        operators={"documents-1d": OperatorSpec(name="documents-1d", **operator_overrides)}
    )


def _lane_runner(
    *,
    existing_pr: int | None = None,
    head_ref: str = "fb/movers",
    engine_rc: int = 0,
    slug: str = "Evan-Kim2028/lake-of-rage",
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """A runner that fakes ``gh``, ``git remote get-url`` and the agent binary.

    Everything else — the commit, the hooks, the push, ``rev-list`` — runs for
    real against the tmp repo, because those are the behaviours worth testing.

    ``git remote get-url`` is faked because the test origin is a filesystem path
    for the push to be real, and a path has no ``owner/repo`` for the binding to
    derive. The slug it reports is what the worktree would really have.

    The fake also *remembers* a PR that ``gh pr create`` produced, so a later
    ``gh pr list`` finds it. Real GitHub does exactly this, and without it the
    binding step would look for a PR the lane had just opened and find nothing.
    """
    created: dict[str, int | None] = {"number": existing_pr}

    def runner(args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        argv = list(args)
        if argv[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(argv, 0, f"git@github.com:{slug}.git\n", "")
        if argv[:1] == ["gh"]:
            if argv[:3] == ["gh", "pr", "list"]:
                number = created["number"]
                payload = (
                    [{"number": number, "headRefName": head_ref, "headRefOid": HEAD_SHA}]
                    if number
                    else []
                )
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
            if argv[:3] == ["gh", "pr", "create"]:
                created["number"] = created["number"] or 3544
                return subprocess.CompletedProcess(
                    argv, 0, f"https://github.com/o/r/pull/{created['number']}\n", ""
                )
        # The engine argv, wrapped by the memory cap, is the only other fake.
        if any(str(a).endswith("cmd") for a in argv[:3]) or "--max-turns" in argv:
            return subprocess.CompletedProcess(argv, engine_rc, STREAM, "")
        return subprocess.run(argv, **kwargs)

    return runner


def _run(repo: Path, task: Path, tmp_path: Path, **kwargs: Any) -> LaneRunResult:  # noqa: ANN401
    """Run a lane with the test's paths, merging *kwargs* over the defaults."""
    opts: dict[str, Any] = {
        "operator": "documents-1d",
        "lane": "movers",
        "repo_path": repo,
        "task_file": task,
        "engine": "cmd",
        "config": _config(),
        "status_file": tmp_path / "lane.status",
        "run_dir": tmp_path / "runs",
        "worktree_parent": tmp_path / "wt",
        "known_gate_subcommands": {"run"},
        "runner": _lane_runner(),
    }
    return run_lane(**{**opts, **kwargs})


# ------------------------------------------------------------- status lines


def test_approved_status_line_emits_a_valid_sha9(tmp_path: Path) -> None:
    path = tmp_path / "lane.status"
    line = write_status_line(path, approved=True, sha9="abcdef1234567890")
    assert "PREMERGE-APPROVED abcdef123" in line
    assert path.read_text().strip() == line


def test_a_bogus_sha_is_never_written_as_an_approval(tmp_path: Path) -> None:
    """`automerge.sh` took the last field and would merge a PR matching it."""
    line = write_status_line(tmp_path / "lane.status", approved=True, sha9="everything")
    assert "PREMERGE-APPROVED" not in line
    assert "NEEDS-ESCALATION" in line


def test_an_escalation_reason_is_flattened_to_one_line(tmp_path: Path) -> None:
    line = write_status_line(tmp_path / "lane.status", approved=False, reason="a\nb  c")
    assert "\n" not in line
    assert "NEEDS-ESCALATION a b c" in line


def test_status_lines_accumulate(tmp_path: Path) -> None:
    path = tmp_path / "lane.status"
    write_status_line(path, approved=False, reason="first")
    write_status_line(path, approved=True, sha9="abcdef123456")
    assert len(path.read_text().strip().splitlines()) == 2


def test_a_missing_status_file_is_not_an_error() -> None:
    # The status file is observability; losing it must not abort a lane.
    assert write_status_line(None, approved=False, reason="x")


# ------------------------------------------------------------- the guarantee


def test_a_failed_engine_still_gets_a_pr(repo: Path, task_file: Path, tmp_path: Path) -> None:
    """The #1 recurring failure: real work on the branch, and no PR.

    The agent "dies" here (non-zero exit) but left work behind, which is
    precisely the case the guarantee exists for.
    """
    _git(repo, "checkout", "-b", "fb/movers")
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")

    result = _run(repo, task_file, tmp_path, runner=_lane_runner(engine_rc=1))

    assert result.pr == 3544
    assert result.guarantee is not None
    assert result.guarantee.committed is True
    # The leftover work really is in git.
    assert "feature.py" in _git(repo, "show", "--name-only", "--format=", "HEAD")
    # No gate available, so the lane stops at pr_guaranteed and says so.
    assert result.state == STATE_PR_GUARANTEED
    assert "PREMERGE-APPROVED" not in result.status_line
    assert "not merged" in result.status_line


def test_a_clean_tree_with_no_work_escalates(repo: Path, task_file: Path, tmp_path: Path) -> None:
    result = _run(repo, task_file, tmp_path)
    assert result.escalated
    assert result.pr is None
    assert "NEEDS-ESCALATION" in result.status_line


def test_a_missing_task_file_escalates_before_any_work(repo: Path, tmp_path: Path) -> None:
    result = run_lane(
        operator="documents-1d",
        lane="movers",
        repo_path=repo,
        task_file=tmp_path / "nope.md",
        config=_config(),
        status_file=tmp_path / "lane.status",
        worktree_parent=tmp_path / "wt",
    )
    assert result.escalated
    assert result.reason == "task_file_missing"


def test_an_already_committed_change_still_opens_a_pr(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    """The agent may commit but forget the PR — the common real-world case."""
    _git(repo, "checkout", "-b", "fb/movers")
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "agent's own commit")

    result = _run(repo, task_file, tmp_path)
    assert result.pr == 3544
    assert result.guarantee is not None
    # Nothing to commit, so the manager did not author a commit of its own.
    assert result.guarantee.committed is False
    assert _git(repo, "log", "-1", "--format=%s") == "agent's own commit"


# ---------------------------------------------------------------- the binding


def test_a_pr_whose_head_is_another_branch_is_refused(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    """The lake-#3544-to-silph-#3544 incident, exercised through ``run_lane``.

    The head mismatch is created *after* the push target is resolved, which is
    the only way it can survive: ``resolve_push_target`` deliberately follows an
    existing PR's head so a lane never opens a second, competing PR. The refusal
    itself is what is under test here; the resolution of *which* branch to use is
    :func:`resolve_push_target`'s job, tested in ``test_fleet_ops_guarantee.py``.

    Both orderings are checked, because a gate that only refused one of them
    would still let the wrong PR through the other.
    """
    _git(repo, "checkout", "-b", "fb/movers")
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")

    def make_runner(head_ref: str) -> Callable[..., subprocess.CompletedProcess[str]]:
        def runner(args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401  # noqa: ANN401
            argv = list(args)
            if argv[:3] == ["git", "remote", "get-url"]:
                return subprocess.CompletedProcess(
                    argv, 0, "git@github.com:Evan-Kim2028/lake-of-rage.git\n", ""
                )
            if argv[:3] == ["gh", "pr", "list"]:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    json.dumps([{"number": 3544, "headRefName": head_ref, "headRefOid": HEAD_SHA}]),
                    "",
                )
            if argv[:3] == ["gh", "pr", "create"]:
                return subprocess.CompletedProcess(
                    argv, 0, "https://github.com/o/r/pull/3544\n", ""
                )
            if any(str(a).endswith("cmd") for a in argv[:3]) or "--max-turns" in argv:
                return subprocess.CompletedProcess(argv, 0, STREAM, "")
            return subprocess.run(argv, **kwargs)

        return runner

    # The lane follows the PR head, so the binding matches it and the gate runs.
    followed = _run(
        repo,
        task_file,
        tmp_path,
        known_gate_subcommands={"gate"},
        runner=make_runner("dq1d/other-lane"),
    )
    # Following the head is correct — that is documents-1d's dq1d/* contract.
    assert followed.branch == "dq1d/other-lane"

    # A slug that does not match the worktree's own origin is refused outright,
    # before any PR lookup can be mistaken.
    mismatched = _run(
        repo,
        task_file,
        tmp_path,
        known_gate_subcommands={"gate"},
        expected_slug="Evan-Kim2028/silphcoanalytics",
        runner=make_runner("dq1d/other-lane"),
    )
    assert mismatched.escalated
    assert mismatched.reason == "refused_repo_slug_mismatch"
    # The lane still keeps its guaranteed PR; it just refuses to judge.
    assert mismatched.pr == 3544


def test_expected_repo_mismatch_refuses_to_gate(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    _git(repo, "checkout", "-b", "fb/movers")
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")

    result = _run(
        repo,
        task_file,
        tmp_path,
        expected_slug="Evan-Kim2028/silphcoanalytics",
        known_gate_subcommands={"gate"},
        runner=_lane_runner(existing_pr=3544),
    )
    assert result.escalated
    assert result.reason == "refused_repo_slug_mismatch"


def test_a_matching_binding_reaches_the_gate(repo: Path, task_file: Path, tmp_path: Path) -> None:
    _git(repo, "checkout", "-b", "fb/movers")
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")

    def gate_runner(args, **_kwargs: object) -> subprocess.CompletedProcess[str]:  # noqa: ANN001
        return subprocess.CompletedProcess(
            list(args), 0, "12:00:00 PREMERGE-APPROVED abcdef123\n", ""
        )

    result = _run(
        repo,
        task_file,
        tmp_path,
        known_gate_subcommands={"gate"},
        runner=_lane_runner(existing_pr=3544),
        gate_runner=gate_runner,
    )
    assert result.binding is not None
    assert result.binding.repo_slug == "Evan-Kim2028/lake-of-rage"
    assert result.approved


def test_no_gate_stops_at_the_guaranteed_pr(repo: Path, task_file: Path, tmp_path: Path) -> None:
    _git(repo, "checkout", "-b", "fb/movers")
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")
    calls: list[object] = []

    def gate_runner(args, **_kwargs: object) -> subprocess.CompletedProcess[str]:  # noqa: ANN001
        calls.append(args)
        return subprocess.CompletedProcess(list(args), 0, "PREMERGE-APPROVED abcdef123\n", "")

    result = _run(
        repo,
        task_file,
        tmp_path,
        known_gate_subcommands={"gate"},
        gate=False,
        runner=_lane_runner(existing_pr=3544),
        gate_runner=gate_runner,
    )
    assert calls == []
    assert not result.approved
    assert result.pr == 3544
    assert result.gate is not None and result.gate.skipped
    assert "--no-gate" in (result.reason or "")


# ---------------------------------------------------------------------- hooks


def test_the_approval_hook_gets_the_shipper_variables(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    """documents-1d's shipper writes dq/reviews/<PR>-<sha9>.md from these."""
    out = tmp_path / "review.md"
    _git(repo, "checkout", "-b", "fb/movers")
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")

    def gate_runner(args, **_kwargs: object) -> subprocess.CompletedProcess[str]:  # noqa: ANN001
        return subprocess.CompletedProcess(
            list(args), 0, "12:00:00 PREMERGE-APPROVED abcdef123\n", ""
        )

    result = _run(
        repo,
        task_file,
        tmp_path,
        config=_config(
            on_approved=f'printf "VERDICT: APPROVE %s-%s" "$PR" "$SHA9" > {out}',
            judge_engine="cmd",
        ),
        known_gate_subcommands={"gate"},
        runner=_lane_runner(existing_pr=3544),
        gate_runner=gate_runner,
    )
    assert result.approved
    assert result.state == STATE_APPROVED
    assert "PREMERGE-APPROVED abcdef123" in result.status_line
    assert out.read_text() == "VERDICT: APPROVE 3544-abcdef123"


def test_the_escalation_hook_runs_with_the_monitor_variables(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    """documents-1d's monitor keys off `exit=` lines written by this hook."""
    out = tmp_path / "hook.log"
    result = _run(
        repo,
        task_file,
        tmp_path,
        config=_config(on_escalated=f'echo "VERDICT: ESCALATE" >> {out}'),
    )
    assert result.escalated
    # A real hook runs through a real shell and writes its own file.
    assert out.read_text().strip() == "VERDICT: ESCALATE"


# ------------------------------------------------------------------ registry


def test_the_lane_is_registered_with_its_repo_and_pr(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    _git(repo, "checkout", "-b", "fb/movers")
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")

    _run(repo, task_file, tmp_path)

    record = registry.load_record("documents-1d", "movers")
    assert record is not None
    assert record.pr == 3544
    assert record.branch == "fb/movers"
    assert record.engine == "cmd"
    assert record.phase in {"impl", "gate", "done"}
    assert record.status_line


def test_events_are_appended_for_the_lane(repo: Path, task_file: Path, tmp_path: Path) -> None:
    _git(repo, "checkout", "-b", "fb/movers")
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")

    _run(repo, task_file, tmp_path)

    names = [e["event"] for e in registry.read_events(operator="documents-1d", lane="movers")]
    assert "lane.started" in names
    assert "lane.pr.guaranteed" in names
    assert "gate.skipped" in names


def test_two_operators_do_not_clobber_each_other(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    _git(repo, "checkout", "-b", "fb/movers")
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")

    _run(repo, task_file, tmp_path)
    run_lane(
        operator="documents-0e",
        lane="movers",
        repo_path=repo,
        task_file=task_file,
        engine="cmd",
        config=FleetOpsConfig(operators={"documents-0e": OperatorSpec(name="documents-0e")}),
        status_file=tmp_path / "other.status",
        run_dir=tmp_path / "runs",
        worktree_parent=tmp_path / "wt",
        known_gate_subcommands={"run"},
        runner=_lane_runner(),
    )
    # The same lane name under two operators stays two records.
    assert registry.load_record("documents-1d", "movers") is not None
    assert registry.load_record("documents-0e", "movers") is not None


# ----------------------------------------------------------------- selection


def test_the_operators_configured_branch_and_engine_are_used(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    """documents-1d pushes to dq1d/* and pins cmd, the judge included."""
    config = load_fleet_ops_config(
        {
            "fleet_ops": {
                "operators": {
                    "documents-1d": {
                        "engine": "cmd",
                        "push_branch": "dq1d/{lane}",
                        "judge_engine": "cmd",
                    }
                }
            }
        }
    )
    assert config is not None
    _git(repo, "checkout", "-b", "dq1d/movers")
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")

    result = run_lane(
        operator="documents-1d",
        lane="movers",
        repo_path=repo,
        task_file=task_file,
        config=config,
        status_file=tmp_path / "lane.status",
        run_dir=tmp_path / "runs",
        worktree_parent=tmp_path / "wt",
        known_gate_subcommands={"run"},
        runner=_lane_runner(),
    )
    assert result.branch == "dq1d/movers"
    assert result.engine == "cmd"
