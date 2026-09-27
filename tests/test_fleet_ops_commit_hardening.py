"""Commit hardening: fixers, scratch exclusion, verified baseline skips, dirty exits.

The failure this replaces is a lane that exits 0 holding four to fifteen
changed files, with no commit, no PR and no status line. Three causes, and each
one is a test here:

1. repo-wide hooks that fail on the base branch's own debt, so every lane's
   commit is refused for something the lane did not write;
2. the implementer's unformatted, comment-laden files, which the repo's own
   style hooks then reject;
3. the agent's scratch directories ending up staged and failing the commit.

Every test uses a real git repository in ``tmp_path`` and a real (shelled) hook,
because all three of those are properties of git and of a subprocess and no
amount of mocking would exercise them. ``gh`` is the only thing stubbed — it
cannot run offline.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet.fleet_ops import guarantee as g
from agent_fleet.fleet_ops.config import (
    DEFAULT_SCRATCH_EXCLUDES,
    FleetOpsConfig,
    load_fleet_ops_config,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo on main with one commit, and a local bare origin to push to."""
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

    _git(root, "checkout", "-b", "fb/lane")
    return root


def _fake_gh(*, created_pr: int | None = 3510) -> Callable[..., Any]:
    """A runner that answers the ``gh`` calls and defers everything else to git.

    It has to pass non-``gh`` commands through *as they arrived*: the fixer
    runner hands over a single shell string, and splitting that into argv
    characters would run a command nobody wrote.
    """

    def run(args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if isinstance(args, (list, tuple)):
            argv = list(args)
            if argv and argv[0] == "gh":
                return subprocess.CompletedProcess(argv, 0, _gh_stdout(argv, created_pr), "")
            return subprocess.run(argv, **kwargs)
        if isinstance(args, str) and args.startswith("gh "):
            return subprocess.CompletedProcess(args, 0, _gh_stdout(args.split(), created_pr), "")
        return subprocess.run(args, **kwargs)

    return run


def _gh_stdout(argv: list[str], created_pr: int | None) -> str:
    if "create" in argv and created_pr is not None:
        return f"https://github.com/o/r/pull/{created_pr}"
    return "[]"


# --------------------------------------------------------------- fixers


def test_fixers_rewrite_the_lanes_own_files_before_the_commit(repo: Path) -> None:
    """An unformatted file is fixed, not committed and then rejected.

    The fixer script lives outside the worktree on purpose: a fixer that rewrote
    its own source would be a fix the repo did not ask for, and the test would
    then be measuring that accident instead of the behaviour.
    """
    (repo / "feature.py").write_text("x   =    1\n", encoding="utf-8")
    fixer = repo.parent / "fix.sh"
    fixer.write_text("#!/bin/sh\nsed -i 's/[[:space:]][[:space:]]*/ /g' \"$1\"\n", encoding="utf-8")
    fixer.chmod(0o755)

    result = g.ensure_pull_request(
        repo,
        branch="fb/lane",
        base="main",
        engine="cmd",
        lane="lane",
        fixers=(f"sh {fixer} {{py}}",),
        scratch_excludes=DEFAULT_SCRATCH_EXCLUDES,
        runner=_fake_gh(),
    )
    assert result.committed is True
    assert (repo / "feature.py").read_text(encoding="utf-8") == "x = 1\n"


def test_a_fixer_that_fails_does_not_fail_the_lane(repo: Path) -> None:
    """The commit is the authority on the files; a fixer's exit code is not.

    A fixer that cannot do its job leaves the files as they were, and the
    commit then either passes or names the real problem. Turning the fixer's
    own non-zero into a lane escalation would fail a lane over a tool's
    opinion of itself.
    """
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")
    result = g.ensure_pull_request(
        repo,
        branch="fb/lane",
        base="main",
        engine="cmd",
        lane="lane",
        fixers=("exit 3",),
        runner=_fake_gh(),
    )
    assert result.committed is True


def test_fixers_never_touch_scratch(repo: Path) -> None:
    """A fixer must not rewrite the agent's own session state.

    Rewriting the config the running agent reads back can break the session
    that produced the work, and no lane has ever needed that file formatted.
    """
    scratch = repo / ".commandcode"
    scratch.mkdir()
    (scratch / "taste.md").write_text("a   =  1\n", encoding="utf-8")
    (repo / "feature.py").write_text("x   =    1\n", encoding="utf-8")

    seen: list[str] = []

    def record(args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        seen.append(str(args))
        if "sed" in str(args):
            return subprocess.CompletedProcess(args, 0, "", "")
        argv = list(args) if isinstance(args, (list, tuple)) else [args]
        return subprocess.run(argv, **kwargs)

    g.run_fixers(
        repo,
        g.changed_files(repo, scratch_excludes=DEFAULT_SCRATCH_EXCLUDES),
        fixers=("sed -i s/1/2/ {py}",),
        runner=record,
    )
    assert seen == ["sed -i s/1/2/ feature.py"]


# ----------------------------------------------------- scratch exclusion


def test_agent_scratch_is_never_staged(repo: Path) -> None:
    """``.commandcode/`` and ``%h/`` are byproducts; staging them fails the commit."""
    for name in (".commandcode", "%h"):
        directory = repo / name
        directory.mkdir()
        (directory / "session.log").write_text("scratch\n", encoding="utf-8")
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")

    result = g.ensure_pull_request(
        repo,
        branch="fb/lane",
        base="main",
        engine="cmd",
        lane="lane",
        scratch_excludes=DEFAULT_SCRATCH_EXCLUDES,
        runner=_fake_gh(),
    )
    assert result.committed is True

    committed = _git(repo, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert committed == ["feature.py"]
    # Excluded means left behind, not deleted.
    assert (repo / ".commandcode" / "session.log").exists()
    assert (repo / "%h" / "session.log").exists()


def test_scratch_that_ignores_git_status_is_still_kept_out(repo: Path) -> None:
    """A *.gitignore'd scratch file must not reach the commit either.

    Unstage-after-add is what makes this true. An untracked-but-ignored file
    never reaches the index through ``git add -A`` anyway, but a scratch file
    the repo *tracks* does, and that is the case the reset covers.
    """
    tracked = repo / "%h"
    tracked.mkdir()
    (tracked / "keep.txt").write_text("tracked scratch\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "track the scratch")
    (tracked / "keep.txt").write_text("edited scratch\n", encoding="utf-8")
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")

    result = g.ensure_pull_request(
        repo,
        branch="fb/lane",
        base="main",
        engine="cmd",
        lane="lane",
        scratch_excludes=DEFAULT_SCRATCH_EXCLUDES,
        runner=_fake_gh(),
    )
    assert result.committed is True
    committed = _git(repo, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert committed == ["feature.py"]
    # The edit survives in the worktree; it was unstaged, not reverted.
    assert (tracked / "keep.txt").read_text(encoding="utf-8") == "edited scratch\n"


def test_run_logs_stay_out_even_with_no_scratch_config(repo: Path) -> None:
    """The run-log exclusion is unconditional and predates ``scratch_excludes``."""
    logs = repo / g.RUN_DIR_LOGS
    logs.mkdir(parents=True)
    (logs / "stream.log").write_text("transcript\n", encoding="utf-8")
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")

    result = g.ensure_pull_request(
        repo, branch="fb/lane", base="main", engine="cmd", lane="lane", runner=_fake_gh()
    )
    assert result.committed is True
    committed = _git(repo, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert committed == ["feature.py"]


# ------------------------------------------------- verified baseline skip


def _hook(repo: Path, body: str) -> None:
    path = repo / ".git" / "hooks" / "pre-commit"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def test_a_baseline_hook_that_is_red_on_its_own_diff_is_not_skipped(repo: Path) -> None:
    """Red on the lane's files is the lane's problem, whatever the config says."""
    _hook(
        repo,
        "#!/bin/sh\necho 'your diff is ugly' >&2\n"
        "printf '[hook]\\n- hook id: no-inline-comments\\n'\nexit 1\n",
    )
    (repo / "feature.py").write_text("# a comment\nx = 1\n", encoding="utf-8")

    result = g.ensure_pull_request(
        repo,
        branch="fb/lane",
        base="main",
        engine="cmd",
        lane="lane",
        baseline_hooks=("no-inline-comments",),
        runner=_fake_gh(),
    )
    assert result.escalated is True
    assert result.reason == "commit_failed"
    assert result.hooks_failed == ["no-inline-comments"]
    assert result.hooks_skipped == []
    assert "your diff is ugly" in result.detail


def test_a_failure_naming_a_non_baseline_hook_is_never_retried(repo: Path) -> None:
    """One real hook in the list means the commit is unpublishable — no partial skip."""
    _hook(
        repo,
        "#!/bin/sh\n"
        "printf '[hook]\\n- hook id: no-inline-comments\\n- hook id: timer-inventory-check\\n'\n"
        "exit 1\n",
    )
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")

    result = g.ensure_pull_request(
        repo,
        branch="fb/lane",
        base="main",
        engine="cmd",
        lane="lane",
        baseline_hooks=("no-inline-comments",),
        runner=_fake_gh(),
    )
    assert result.escalated is True
    assert "timer-inventory-check" in result.hooks_failed
    assert result.hooks_skipped == []


def test_the_bypass_is_recorded_in_the_commit_and_the_pr_body() -> None:
    """A skipped hook is invisible in the diff, so it has to be stated in prose."""
    body = g._default_pr_body(
        lane="lane", engine="cmd", task_file=None, hooks_skipped=["no-inline-comments"]
    )
    assert "no-inline-comments" in body
    assert "pre-existing debt" in body

    message = g.build_commit_message("cmd", lane="lane", hooks_skipped=["no-inline-comments"])
    assert "no-inline-comments" in message


def test_no_skip_is_recorded_when_every_hook_passed() -> None:
    """Silence means every hook ran. Stating a skip that never happened would be a lie."""
    assert "Skipped" not in g.build_commit_message("cmd", lane="lane")
    assert "Baseline hooks skipped" not in g._default_pr_body(
        lane="lane", engine="cmd", task_file=None
    )


# ------------------------------------------------------------ config parse


def test_the_new_keys_parse_and_default_sensibly() -> None:
    """Every key optional; the defaults must not block a repo that says nothing."""
    assert load_fleet_ops_config({}) is None
    assert load_fleet_ops_config({"fleet_ops": False}) is None

    minimal = load_fleet_ops_config({"fleet_ops": {"base_branch": "trunk"}})
    assert minimal is not None
    assert minimal.pre_commit_fixers == ()
    assert minimal.baseline_hooks == ()
    assert minimal.scratch_excludes == DEFAULT_SCRATCH_EXCLUDES

    full = load_fleet_ops_config(
        {
            "fleet_ops": {
                "baseline_hooks": ["no-inline-comments", "pyright"],
                "pre_commit_fixers": ["ruff format {py}"],
                "scratch_excludes": [".commandcode/"],
            }
        }
    )
    assert full is not None
    assert full.baseline_hooks == ("no-inline-comments", "pyright")
    assert full.pre_commit_fixers == ("ruff format {py}",)
    assert full.scratch_excludes == (".commandcode/",)


def test_an_explicit_empty_scratch_list_is_honoured() -> None:
    """``[]`` means "this repo has none"; only an absent key takes the default."""
    cfg = load_fleet_ops_config({"fleet_ops": {"scratch_excludes": []}})
    assert cfg is not None
    assert cfg.scratch_excludes == ()


def test_baseline_hook_ids_unions_both_spellings() -> None:
    """A repo that used the old key gets the new behaviour, not the old one."""
    cfg = FleetOpsConfig(
        baseline_hooks=("no-inline-comments",),
        baseline_skip_hooks=("pyright", "no-inline-comments"),
    )
    assert cfg.baseline_hook_ids() == ("no-inline-comments", "pyright")
