"""The PR guarantee — the failure this lane exists to eliminate.

These tests use real git repositories in ``tmp_path`` (so the commit/hook
behaviour is genuine) and a fake ``runner`` only for the ``gh`` calls, which
cannot be exercised offline. The important assertions are about git: that a
leftover change really is committed, that the commit really runs hooks, and that
``SKIP=`` is honoured for exactly the named ids.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet.fleet_ops import guarantee as g
from agent_fleet.fleet_ops.guarantee import (
    build_commit_message,
    ensure_pull_request,
    is_dirty,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A tiny git repo with one commit on main, plus a local bare origin.

    The bare remote means the push in the guarantee is a *real* push to a *real*
    remote, so "the branch actually got published" is tested rather than mocked.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "lane@example.com")
    _git(root, "config", "user.name", "Lane Manager")
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


def _fake_gh(
    *,
    existing_pr: int | None = None,
    created_pr: int | None = 3510,
    ahead: int = 1,
    head_ref: str | None = None,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """A runner that fakes only ``gh``.

    Git is NOT intercepted: the commit, the hook behaviour and the push all run
    for real against the tmp repo, because those are exactly the behaviours
    worth testing. Only ``gh`` (which needs the network) and the
    ``git rev-list`` commit-count probe are stubbed.
    """

    def runner(args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        argv = list(args)
        if argv and argv[0] == "gh":
            if "list" in argv:
                payload = (
                    [{"number": existing_pr, "headRefName": head_ref or "fb/lane"}]
                    if existing_pr
                    else []
                )
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
            if "create" in argv:
                out = f"https://github.com/o/r/pull/{created_pr}\n" if created_pr else ""
                return subprocess.CompletedProcess(argv, 0, out, "")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "rev-list" in argv:
            return subprocess.CompletedProcess(argv, 0, str(ahead), "")
        return subprocess.run(argv, **kwargs)

    return runner


# --------------------------------------------------------------- the guarantee


def test_dirty_tree_is_committed_and_a_pr_is_opened(repo: Path) -> None:
    branch = "fb/lane"
    _git(repo, "checkout", "-b", branch)
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")

    result = ensure_pull_request(
        repo,
        branch=branch,
        base="main",
        engine="cmd",
        lane="lane",
        task_file=None,
        runner=_fake_gh(created_pr=3510),
    )

    assert result.committed is True
    assert result.pr == 3510
    assert result.guaranteed is True
    assert result.reason == "pr_created"
    # The change really is in git now.
    assert "feature.py" in _git(repo, "show", "--name-only", "--format=", "HEAD")


def test_auto_commit_uses_the_documented_subject(repo: Path) -> None:
    branch = "fb/lane"
    _git(repo, "checkout", "-b", branch)
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")

    ensure_pull_request(
        repo,
        branch=branch,
        base="main",
        engine="devin",
        lane="lane",
        task_file=None,
        runner=_fake_gh(),
    )

    subject = _git(repo, "log", "-1", "--format=%s")
    assert subject == "fleet: auto-commit after devin run"


def test_clean_tree_with_an_existing_pr_does_not_commit(repo: Path) -> None:
    branch = "fb/lane"
    _git(repo, "checkout", "-b", branch)
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "agent commit")

    result = ensure_pull_request(
        repo,
        branch=branch,
        base="main",
        engine="cmd",
        lane="lane",
        runner=_fake_gh(existing_pr=777),
    )

    assert result.committed is False
    assert result.pr == 777
    assert result.reason == "existing PR"


def test_no_work_escalates_with_a_reason(repo: Path) -> None:
    branch = "fb/lane"
    _git(repo, "checkout", "-b", branch)

    result = ensure_pull_request(
        repo,
        branch=branch,
        base="main",
        engine="cmd",
        lane="lane",
        runner=_fake_gh(created_pr=None, ahead=0),
    )

    assert result.pr is None
    assert result.escalated is True
    assert result.reason == "no_commits_ahead"
    assert "no publishable work" in result.detail


def test_guarantee_is_idempotent(repo: Path) -> None:
    branch = "fb/lane"
    _git(repo, "checkout", "-b", branch)
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")

    first = ensure_pull_request(
        repo, branch=branch, base="main", engine="cmd", lane="lane", runner=_fake_gh()
    )
    second = ensure_pull_request(
        repo,
        branch=branch,
        base="main",
        engine="cmd",
        lane="lane",
        runner=_fake_gh(existing_pr=3510),
    )

    assert first.pr == 3510
    assert second.pr == 3510
    assert second.committed is False


def test_missing_worktree_escalates(tmp_path: Path) -> None:
    result = ensure_pull_request(
        tmp_path / "nope",
        branch="fb/lane",
        base="main",
        engine="cmd",
        lane="lane",
        runner=_fake_gh(),
    )
    assert result.escalated is True
    assert result.reason == "worktree_missing"


# ------------------------------------------------------------------ hooks on


def test_commit_refuses_no_verify(repo: Path) -> None:
    """The guard must be structural, not a convention."""
    with pytest.raises(ValueError, match="hooks must stay enabled"):
        g._git(["git", "commit", "--no-verify", "-m", "x"], cwd=repo)


def test_a_failing_hook_blocks_the_commit_and_escalates(repo: Path) -> None:
    """Hooks stay live: a real hook failure is a real problem, not bypassed."""
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho 'hook says no' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    branch = "fb/lane"
    _git(repo, "checkout", "-b", branch)
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")

    result = ensure_pull_request(
        repo, branch=branch, base="main", engine="cmd", lane="lane", runner=_fake_gh()
    )

    assert result.escalated is True
    assert result.reason == "commit_failed"
    assert "hook says no" in result.detail
    # Nothing was committed — the hook did its job.
    assert _git(repo, "log", "-1", "--format=%s") == "base"


def test_named_baseline_hook_is_skipped_but_others_still_run(repo: Path) -> None:
    """SKIP= is narrow: it silences the named hook, and only that one."""
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    for name, body in (
        ("pre-commit", "#!/bin/sh\nexit 0\n"),
        ("post-commit", "#!/bin/sh\nexit 0\n"),
    ):
        path = hooks / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    # A pre-commit hook that fails unless SKIP names *this* hook id — which is
    # how pre-commit's own selective-skip works. A stub that merely tested
    # `[ -n "$SKIP" ]` could not tell "skipped me" from "skipped something else",
    # and would pass for a manager that silenced every hook at once.
    pre = hooks / "pre-commit"
    pre.write_text(
        "#!/bin/sh\n"
        'case ",${SKIP}," in *,pre-commit,*) exit 0;; esac\n'
        "echo 'baseline-red hook' >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    pre.chmod(0o755)

    branch = "fb/lane"
    _git(repo, "checkout", "-b", branch)
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")

    ok = ensure_pull_request(
        repo,
        branch=branch,
        base="main",
        engine="cmd",
        lane="lane",
        skip_hooks=("pre-commit",),
        runner=_fake_gh(),
    )
    assert ok.committed is True
    assert ok.skip_env.get("SKIP") == "pre-commit"

    # A hook NOT in the list still runs and can still block.
    (repo / "other.py").write_text("y = 2\n", encoding="utf-8")
    blocked = ensure_pull_request(
        repo,
        branch=branch,
        base="main",
        engine="cmd",
        lane="lane",
        skip_hooks=("some-other-hook",),
        runner=_fake_gh(),
    )
    assert blocked.escalated is True
    assert blocked.reason == "commit_failed"
    assert "baseline-red hook" in blocked.detail


def test_skip_env_is_reported_on_the_result(repo: Path) -> None:
    branch = "fb/lane"
    _git(repo, "checkout", "-b", branch)
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")
    result = ensure_pull_request(
        repo,
        branch=branch,
        base="main",
        engine="cmd",
        lane="lane",
        skip_hooks=("ruff-format", "pyright"),
        runner=_fake_gh(),
    )
    assert result.skip_env == {"SKIP": "ruff-format,pyright"}


# ------------------------------------------------------------------ helpers


def test_is_dirty_reflects_the_worktree(repo: Path) -> None:
    assert is_dirty(repo) is False
    (repo / "new.txt").write_text("hi\n", encoding="utf-8")
    assert is_dirty(repo) is True
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "x")
    assert is_dirty(repo) is False


def test_commit_message_records_provenance() -> None:
    msg = build_commit_message("cmd", task_file="/p/lane.task.md", lane="lane")
    assert msg.splitlines()[0] == "fleet: auto-commit after cmd run"
    assert "lane" in msg
    assert "/p/lane.task.md" in msg


def test_resolve_push_target_prefers_an_existing_pr_head(repo: Path) -> None:
    branch, why = g.resolve_push_target(
        "fb/lane",
        cwd=repo,
        configured="fb/other",
        runner=_fake_gh(existing_pr=5, head_ref="fb/real"),
    )
    assert branch == "fb/real"
    assert "overrides" in why


def test_resolve_push_target_uses_configured_when_no_pr(repo: Path) -> None:
    branch, why = g.resolve_push_target(
        "fb/lane", cwd=repo, configured="fb/other", runner=_fake_gh(existing_pr=None)
    )
    assert branch == "fb/other"
    assert "configured" in why


def test_pr_head_ref_reads_the_open_pr(repo: Path) -> None:
    assert (
        g.pr_head_ref("fb/lane", cwd=repo, runner=_fake_gh(existing_pr=3, head_ref="fb/x"))
        == "fb/x"
    )
    assert g.pr_head_ref("fb/lane", cwd=repo, runner=_fake_gh(existing_pr=None)) is None


def test_pr_title_prefers_the_task_heading(tmp_path: Path) -> None:
    task = tmp_path / "lane.task.md"
    task.write_text("# Fix the stamp column update\n\nbody\n", encoding="utf-8")
    title = g._default_pr_title(lane="lane", engine="cmd", task_file=str(task))
    assert title == "Fix the stamp column update"


def test_pr_title_falls_back_without_a_task_file() -> None:
    assert (
        g._default_pr_title(lane="lane", engine="cmd", task_file=None) == "[fleet/lane] lane (cmd)"
    )


def test_head_sha_reads_the_current_commit(repo: Path) -> None:
    sha = g.head_sha(repo)
    assert sha is not None and len(sha) == 40
    short = g.head_sha(repo, short=9)
    assert short is not None and len(short) == 9
