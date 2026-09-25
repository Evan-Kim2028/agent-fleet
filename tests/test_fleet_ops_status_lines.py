"""Defects 3 and 4: what the status line says when the gate is off, and on hooks.

**3 — the gate-skipped line.** With ``--no-gate`` the lane ended at
``pr_guaranteed`` and then wrote::

    HH:MM:SS NEEDS-ESCALATION PR #3544 guaranteed; gate disabled (--no-gate) ...

An operator scanning the status file reads that as *something went wrong*, and
the merge planner's approval collectors see a lane that needs a human. The
outcome was never an escalation: the PR is guaranteed, the external gate owns
review from here. It gets its own token — ``GATE-SKIPPED PR #<n> @<sha9>
(<reason>)`` — which is machine-readable and still leaves the approval line
untouched.

**4 — the hook-failure detail.** When the guarantee's commit died on a
pre-commit hook, the lane reported ``commit_failed`` with 2000 characters of
hook transcript. The one fact an operator needs — *which* hook — was buried in
it, and the transcript is truncated at an arbitrary offset. The hook ids are
parsed out and carried as ``hooks_failed=[...]``.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet.fleet_ops import gate as gate_mod
from agent_fleet.fleet_ops import guarantee as g
from agent_fleet.fleet_ops.guarantee import failed_hook_ids
from agent_fleet.fleet_ops.registry import STATE_PR_GUARANTEED
from agent_fleet.fleet_ops.runner import LaneRunResult, run_lane
from agent_fleet.fleet_ops.statusfile import GATE_SKIPPED_TOKEN, gate_skipped_line

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

HEAD_SHA = "a" * 40

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
    *, existing_pr: int | None = None, slug: str = "Evan-Kim2028/lake-of-rage"
) -> Callable[..., subprocess.CompletedProcess[str]]:
    created: dict[str, int | None] = {"number": existing_pr}

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
                created["number"] = created["number"] or 3544
                return subprocess.CompletedProcess(
                    argv, 0, f"https://github.com/o/r/pull/{created['number']}\n", ""
                )
        if any(str(a).endswith("cmd") for a in argv[:3]) or "--max-turns" in argv:
            return subprocess.CompletedProcess(argv, 0, STREAM, "")
        return subprocess.run(argv, **kwargs)

    return runner


def _gh_stub() -> Callable[..., subprocess.CompletedProcess[str]]:
    """A runner that fakes only ``gh``; git runs for real."""
    created: dict[str, int | None] = {"number": None}

    def runner(args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        argv = list(args)
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
        return subprocess.run(argv, **kwargs)

    return runner


def _run(repo: Path, task: Path, tmp_path: Path, **kwargs: Any) -> LaneRunResult:  # noqa: ANN401
    from agent_fleet.fleet_ops.config import FleetOpsConfig, OperatorSpec

    hook_failing = bool(kwargs.pop("hook_failing", False))
    if hook_failing:
        # A real pre-commit hook in the worktree's git dir, so the guarantee's
        # `git commit` genuinely fails and the failure text is pre-commit's.
        _install_failing_hook(repo)

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
        "runner": _lane_runner(),
    }
    return run_lane(**{**opts, **kwargs})


def _install_failing_hook(repo: Path) -> None:
    """A pre-commit hook that fails the way pre-commit reports one."""
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-commit"
    hook.write_text(
        "#!/bin/sh\n"
        "echo 'Lane Guard...........................................................Failed' >&2\n"
        "echo '- hook id: lane-guard' >&2\n"
        "echo '- exit code: 1' >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)


def _with_work(repo: Path) -> None:
    _git(repo, "checkout", "-b", "fb/movers")
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")


# ------------------------------------------------------- 3. the gate-skipped line


def test_gate_skipped_line_names_the_pr_and_head() -> None:
    line = gate_skipped_line(3544, sha="abcdef1234567890", reason="gate disabled (--no-gate)")
    assert GATE_SKIPPED_TOKEN in line
    assert "PR #3544" in line
    assert "@abcdef123" in line
    assert "(gate disabled (--no-gate))" in line


def test_a_missing_sha_never_invents_one() -> None:
    """The automerge took the last field as a sha; a placeholder would be read as one."""
    line = gate_skipped_line(3544, sha=None, reason="no gate installed")
    assert "@" not in line
    assert GATE_SKIPPED_TOKEN in line


def test_a_long_reason_is_flattened_to_one_line() -> None:
    line = gate_skipped_line(1, sha="abcdef123", reason="a\nb  c")
    assert "\n" not in line
    assert "a b c" in line


def test_no_gate_writes_gate_skipped_not_an_escalation(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    _with_work(repo)
    calls: list[object] = []

    def gate_runner(args, **_kwargs: object) -> subprocess.CompletedProcess[str]:  # noqa: ANN001
        calls.append(args)
        return subprocess.CompletedProcess(list(args), 0, "PREMERGE-APPROVED abcdef123\n", "")

    result = _run(
        repo,
        task_file,
        tmp_path,
        gate=False,
        runner=_lane_runner(existing_pr=3544),
        gate_runner=gate_runner,
    )

    assert calls == []
    assert result.state == STATE_PR_GUARANTEED
    assert not result.approved
    assert result.pr == 3544
    assert GATE_SKIPPED_TOKEN in result.status_line
    assert "NEEDS-ESCALATION" not in result.status_line
    assert "PR #3544" in result.status_line


def test_an_absent_gate_writes_the_same_token(repo: Path, task_file: Path, tmp_path: Path) -> None:
    """A gate that does not exist is the same outcome as ``--no-gate``, and says so."""
    _with_work(repo)
    result = _run(repo, task_file, tmp_path, known_gate_subcommands={"run"})
    assert GATE_SKIPPED_TOKEN in result.status_line
    assert "NEEDS-ESCALATION" not in result.status_line
    assert result.state == STATE_PR_GUARANTEED


def test_the_gate_skipped_line_lands_in_the_status_file(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    _with_work(repo)
    status = tmp_path / "lane.status"
    result = _run(repo, task_file, tmp_path, gate=False, status_file=status)
    assert result.status_line in status.read_text(encoding="utf-8")


def test_the_approval_line_contract_is_unchanged() -> None:
    """The gate is a *reader* of the approval line; its parser must not drift."""
    assert gate_mod.is_approval_line("12:00:00 PREMERGE-APPROVED abcdef123")
    assert gate_mod.is_approval_line("PREMERGE-APPROVED abcdef1234567890")
    # A gate-skipped line is not an approval and must not be read as one.
    assert not gate_mod.is_approval_line(
        "12:00:00 GATE-SKIPPED PR #3544 @abcdef123 (gate disabled)"
    )
    # Nor is a line that merely contains the token somewhere in prose.
    assert not gate_mod.is_approval_line("12:00:00 lane PREMERGE-APPROVED abcdef123 ok")


def test_the_gate_never_approves_from_a_gate_skipped_line() -> None:
    approved, reason, _ = gate_mod._classify("12:00:00 GATE-SKIPPED PR #3544 @abcdef123 (x)")
    assert approved is False
    assert reason


# ----------------------------------------------------- 4. the hook-failure detail


def test_hook_ids_are_parsed_from_a_pre_commit_failure() -> None:
    output = (
        "Trim Trailing Whitespace.................................................Failed\n"
        "- hook id: trim-trailing-whitespace\n"
        "- exit code: 1\n"
        "\n"
        "Check Yaml.................................................................Failed\n"
        "- hook id: check-yaml\n"
        "- exit code: 1\n"
    )
    assert failed_hook_ids(output) == ["trim-trailing-whitespace", "check-yaml"]


def test_hook_ids_are_deduplicated_in_order() -> None:
    output = "- hook id: ruff-format\nblah\n- hook id: ruff-format\n"
    assert failed_hook_ids(output) == ["ruff-format"]


def test_a_commit_failure_that_is_not_a_hook_reports_no_hook_ids() -> None:
    """``commit_failed`` with no hooks is a different failure; the field must be empty."""
    assert failed_hook_ids("fatal: something else went wrong") == []


def test_the_guarantee_records_the_failing_hook_ids(repo: Path) -> None:
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "checkout", "-b", "fb/movers")
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        "#!/bin/sh\necho 'Trim Trailing Whitespace...Failed' >&2\n"
        "echo '- hook id: trim-trailing-whitespace' >&2\nexit 1\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)

    result = g.ensure_pull_request(
        repo, branch="fb/movers", base="main", engine="cmd", lane="movers"
    )

    assert result.escalated
    assert result.reason == "commit_failed"
    assert result.hooks_failed == ["trim-trailing-whitespace"]
    assert "hooks_failed=[trim-trailing-whitespace]" in result.detail


def test_a_hook_failure_reaches_the_status_line(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    """The status file is all an operator sees; the hook id has to be in the line."""
    _git(repo, "checkout", "-b", "fb/movers")
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")

    result = _run(repo, task_file, tmp_path, hook_failing=True)

    assert result.escalated
    assert result.reason == "commit_failed"
    assert result.guarantee is not None
    assert result.guarantee.hooks_failed == ["lane-guard"]
    assert "hooks_failed=[lane-guard]" in result.status_line
    assert "NEEDS-ESCALATION" in result.status_line


def test_a_baseline_skip_lets_the_commit_through(repo: Path) -> None:
    """``SKIP=`` still works exactly as before: the named hook is the only one skipped."""
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "checkout", "-b", "fb/movers")
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        '#!/bin/sh\n[ "$SKIP" = "lane-guard" ] && exit 0\n'
        "echo '- hook id: lane-guard' >&2\nexit 1\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)

    result = g.ensure_pull_request(
        repo,
        branch="fb/movers",
        base="main",
        engine="cmd",
        lane="movers",
        skip_hooks=("lane-guard",),
        runner=_gh_stub(),
    )

    assert result.committed is True
    assert result.hooks_failed == []
