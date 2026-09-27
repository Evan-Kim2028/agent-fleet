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

import os
import subprocess
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet.fleet_ops import guarantee as g
from agent_fleet.fleet_ops.config import (
    DEFAULT_SCRATCH_EXCLUDES,
    FleetOpsConfig,
    load_fleet_ops_config,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
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


@contextmanager
def _path_with(*dirs: Path) -> Iterator[None]:
    """Temporarily prepend *dirs* to ``PATH``.

    The baseline-skip path shells out to a real ``pre-commit`` command, so a
    shim on ``PATH`` is the only way to exercise it without a network.
    """
    original = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join([*(str(d) for d in dirs), original])
    try:
        yield
    finally:
        os.environ["PATH"] = original


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


def test_a_rename_reports_both_paths_intact(repo: Path) -> None:
    """``git status -z`` splits a rename into two entries; neither may be mangled.

    In ``-z`` mode git emits ``R  <new>\\0<old>\\0`` — the source path arrives as
    its own record with no ``XY `` status columns. Reading every record with a
    blind ``entry[3:]`` therefore truncated the source (``pkg/module_one.py``
    became ``st_module.py``), handing a fixer a path that does not exist and
    silently truncating the list of files the rest of the lane's work lives in.
    """
    (repo / "module_one.py").write_text("a = 1\n", encoding="utf-8")
    (repo / "second.py").write_text("b = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add modules")
    _git(repo, "mv", "module_one.py", "module_renamed.py")
    (repo / "second.py").write_text("b = 3\n", encoding="utf-8")

    files = g.changed_files(repo)

    assert "module_renamed.py" in files, f"rename destination missing from {files}"
    assert "module_one.py" in files, f"rename source missing or truncated in {files}"
    assert "second.py" in files, f"a sibling edit was dropped by the rename in {files}"
    # The source path is a full name, not the tail a blind entry[3:] would leave.
    assert not any(f.startswith("st_module") for f in files), files


def test_a_file_name_is_never_executed_as_shell_syntax(repo: Path) -> None:
    """A file name is an argument, not a command. The agent chooses these names.

    The fixer string is trusted repo config, but ``{py}`` is filled with a path
    the agent created, and it is handed to ``/bin/sh`` through ``shell=True``.
    Substituted bare, ``feature.py$(touch PWNED).py`` is a command substitution
    and runs as the operator — the lane can execute code by naming a file.
    """
    hostile = "feature.py$(touch PWNED).py"
    (repo / hostile).write_text("x = 1\n", encoding="utf-8")

    failed = g.run_fixers(repo, g.changed_files(repo), fixers=("echo fixed {py} > /dev/null",))

    assert failed == [], "the fixer must run cleanly, not be defeated by the name"
    assert not (repo / "PWNED").exists(), "command injection: the file name was executed"


def test_a_fixer_still_reaches_a_name_with_shell_metacharacters(repo: Path) -> None:
    """The injection fix must not be a filter: ordinary awkward names still get fixed.

    ``report (v2).md`` and ``a&b.py`` are not attacks, and before the quoting they
    broke the repo's own fixer — ``/bin/sh: 1: b.py: not found`` — so the lane's
    real files went unfixed. They are fixed now because the path is one word.
    """
    for name in ("report (v2).md", "a&b.py"):
        (repo / name).write_text("x   =   1\n", encoding="utf-8")

    failed = g.run_fixers(repo, g.changed_files(repo), fixers=("sed -i s/1/2/ {py}",), timeout=60)

    assert failed == []
    for name in ("report (v2).md", "a&b.py"):
        assert (repo / name).read_text(encoding="utf-8") == "x   =   2\n", name


def test_a_fixer_without_a_placeholder_is_still_run_once(repo: Path) -> None:
    """A whole-tree fixer is the author's own text; only ``{py}`` gets quoted."""
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")
    seen: list[str] = []

    def record(args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        seen.append(str(args))
        argv = list(args) if isinstance(args, (list, tuple)) else [args]
        return subprocess.run(argv, **kwargs)

    g.run_fixers(repo, ["a.py", "b.py"], fixers=("git add -A",), runner=record)

    assert seen == ["git add -A"]


# ------------------------------------------------------ return-arity contract


def test_every_git_call_in_a_commit_carries_the_skip_overlay(repo: Path) -> None:
    """The overlay belongs to the whole commit, not just to ``git commit``.

    The commit is conducted under ``SKIP=…``; a ``git add``, a ``git status`` or
    a ``git reset`` made without it is a git invocation behaving differently from
    the one the caller asked for, and the contract a repo depends on is that
    every call in the path sees the same environment.
    """
    seen: list[tuple[list[str], dict[str, str] | None]] = []

    def runner(args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        argv = list(args) if isinstance(args, (list, tuple)) else [args]
        seen.append((argv, kwargs.get("env")))
        return subprocess.run(argv, **kwargs)

    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")
    g.commit_worktree(repo, engine="cmd", lane="movers", skip_hooks=("pyright",), runner=runner)

    # The commit path is the status read, the add, the unstaging and the commit
    # itself; `head_sha`'s `git rev-parse` is a read of the result, not a step in
    # producing it.
    steps = [entry for entry in seen if entry[0][1] != "rev-parse"]
    assert len(steps) >= 4, f"the commit path was not exercised: {[a[:2] for a, _ in seen]}"
    for argv, env in steps:
        assert isinstance(env, dict), f"{argv[:2]} ran without the SKIP overlay"
        assert env["SKIP"] == "pyright"
        assert "PATH" in env, "the overlay replaced the environment instead of extending it"


def test_a_five_unpack_is_not_confused_by_an_earlier_two_unpack(repo: Path) -> None:
    """A frame that unpacks something else first still wants five values.

    ``CommitResult`` yields whichever shape the caller's ``UNPACK_SEQUENCE``
    asked for, and reading "the most recent unpack at or before the instruction
    pointer" is wrong on CPython 3.14: the pointer lands on the trailing
    ``STORE_FAST`` of the *previous* statement, so the earlier two-value unpack
    was mistaken for the target and the five-value call died with "not enough
    values to unpack (expected 5, got 2)".
    """
    runner = _refuse_runner()

    def settings() -> tuple[str, int]:
        return ("prod", 2)

    # The decoy and the real unpack must be in ONE frame, or this stops
    # reproducing the shape the defect was reported in.
    name, env = settings()
    ok, sha, detail, failed, skipped = g.commit_worktree(
        repo, engine="cmd", lane="movers", runner=runner
    )
    assert (name, env) == ("prod", 2)
    assert ok is False
    assert sha is None
    assert "git add failed" in detail
    assert (failed, skipped) == ([], [])


def test_a_four_unpack_is_not_confused_by_an_earlier_five_unpack(repo: Path) -> None:
    """The same misreading in the other direction: a 4-unpack after a 5-unpack."""
    runner = _refuse_runner()

    def probe() -> tuple[int, int, int, int, int]:
        return (1, 2, 3, 4, 5)

    a, b, c, d, e = probe()
    ok, sha, detail, failed = g.commit_worktree(repo, engine="cmd", lane="movers", runner=runner)
    assert (a, b, c, d, e) == (1, 2, 3, 4, 5)
    assert ok is False
    assert sha is None
    assert "git add failed" in detail
    assert failed == []


def _refuse_runner() -> Any:  # noqa: ANN401
    """A runner that refuses everything, so the call unwraps without touching git."""

    def run(args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        return subprocess.CompletedProcess(list(args), 1, "", "no")

    return run


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


def test_a_hook_that_was_only_skipped_up_front_is_never_reported_as_verified(
    repo: Path, tmp_path: Path
) -> None:
    """The record may only name a hook this commit actually re-ran and passed.

    ``baseline_hook_ids()`` unions both spellings, so a repo that set
    ``baseline_skip_hooks`` has ids that were silenced before the first commit
    and never ran. Publishing those next to the verified ones in the commit
    message and PR body — which say "clean on this lane's changed files" and
    "re-run against the files this PR changes" — is a false audit trail in the
    one durable record a bypassed hook leaves behind. They are still bypassed;
    they are just not claimed to have been checked.
    """
    _hook(
        repo,
        "#!/bin/sh\n"
        'case ",${SKIP}," in *,pyright,*) exit 0;; esac\n'
        "echo 'baseline debt outside the diff' >&2\n"
        "printf '[hook]\\n- hook id: pyright\\n'\n"
        "exit 1\n",
    )
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "pre-commit"
    shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    shim.chmod(0o755)
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")

    config = FleetOpsConfig(
        baseline_hooks=("pyright",),
        baseline_skip_hooks=("ruff-format",),
    )
    assert config.baseline_hook_ids() == ("pyright", "ruff-format")

    with _path_with(shim_dir):
        result = g.ensure_pull_request(
            repo,
            branch="fb/lane",
            base="main",
            engine="cmd",
            lane="lane",
            skip_hooks=config.baseline_skip_hooks,
            baseline_hooks=config.baseline_hook_ids(),
            runner=_fake_gh(),
        )

    assert result.committed is True
    assert result.hooks_skipped == ["pyright"]
    message = _git(repo, "log", "-1", "--format=%B")
    assert "pyright" in message
    assert "ruff-format" not in message, "an unverified up-front skip was published as verified"
    body = g._default_pr_body(
        lane="lane", engine="cmd", task_file=None, hooks_skipped=result.hooks_skipped
    )
    assert "ruff-format" not in body


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
