"""End-to-end coverage of the gate's test plumbing on a realistic multi-package repo.

Three pilot-found bugs (2026-09-25) shared one root cause — the unit tests only
covered a single root package:

1. a uv-workspace root ran ``uv run pytest`` without ``--all-packages``, so tests
   importing workspace members failed to import in a fresh worktree;
2. the PR diff was taken against the LOCAL base branch, which can be far behind
   the forge, inflating "the PR's changed tests" with unrelated upstream files;
3. tests were grouped per owning package but handed to pytest as repo-relative
   paths while pytest ran with ``cwd=package``, so nothing was collected.

These tests build a synthetic workspace (root + nested ``pipelines/pkg`` + ``api``)
and drive the real runner through a fake ``uv`` that records its argv/cwd and then
runs pytest, so the plumbing is exercised without network access.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agent_fleet.gate.gitops import changed_test_files, prepare_worktree, resolve_diff_base
from agent_fleet.gate.pipeline import GateTestRunner
from agent_fleet.gate.pytest_runner import to_package_path, to_repo_node_id, uv_run_prefix
from agent_fleet.slots import declared_size, record_size

_FAKE_UV = textwrap.dedent(
    """\
    #!{python}
    import json, os, subprocess, sys
    args = sys.argv[1:]
    assert args[0] == "run", args
    args = args[1:]
    all_packages = "--all-packages" in args
    args = [a for a in args if a != "--all-packages"]
    assert args[0] == "pytest", args
    with open(os.environ["FAKE_UV_LOG"], "a") as fh:
        fh.write(json.dumps({{"cwd": os.getcwd(), "all_packages": all_packages,
                              "args": args[1:]}}) + "\\n")
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
                              *args[1:]]))
    """
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    _write(
        root / "pyproject.toml",
        """\
        [project]
        name = "root"
        version = "0"
        [tool.uv.workspace]
        members = ["pipelines/pkg"]
        """,
    )
    _write(root / "tests/test_root.py", "def test_root():\n    assert True\n")
    _write(root / "pipelines/pkg/pyproject.toml", '[project]\nname = "pkg"\nversion = "0"\n')
    _write(
        root / "pipelines/pkg/tests/test_pkg.py",
        "def test_ok():\n    assert True\n\ndef test_fails():\n    assert 1 == 2\n",
    )
    _write(root / "api/pyproject.toml", '[project]\nname = "api"\nversion = "0"\n')
    _write(root / "api/tests/test_api.py", "def test_api():\n    assert True\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    uv = bindir / "uv"
    uv.write_text(_FAKE_UV.format(python=sys.executable), encoding="utf-8")
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_UV_LOG", str(tmp_path / "uv.log"))
    return root


def _uv_log(tmp_path: Path) -> list[dict]:
    path = tmp_path / "uv.log"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_multi_package_run_uses_package_relative_paths_and_repo_relative_ids(
    workspace: Path, tmp_path: Path
) -> None:
    runner = GateTestRunner(root=workspace, use_systemd=False)
    result = runner.run(
        ["tests/test_root.py", "pipelines/pkg/tests/test_pkg.py", "api/tests/test_api.py"]
    )
    assert not result.infra_error, result.infra_error
    assert result.tests_failed
    # Failing node id comes back REPO-relative, ready for verify/fix/recheck.
    assert result.failing == ["pipelines/pkg/tests/test_pkg.py::test_fails"]
    calls = {Path(c["cwd"]).relative_to(workspace).as_posix(): c for c in _uv_log(tmp_path)}
    # Each package ran from its own dir with package-relative test paths.
    assert calls["pipelines/pkg"]["args"][-1] == "tests/test_pkg.py"
    assert calls["api"]["args"][-1] == "tests/test_api.py"
    assert calls["."]["args"][-1] == "tests/test_root.py"
    # Only the uv workspace root gets --all-packages.
    assert calls["."]["all_packages"] is True
    assert calls["pipelines/pkg"]["all_packages"] is False
    assert calls["api"]["all_packages"] is False


def test_pytest_hint_is_runnable_from_the_package_dir(workspace: Path) -> None:
    runner = GateTestRunner(root=workspace, use_systemd=False)
    hint = runner.pytest_hint("pipelines/pkg/tests/test_gate_x.py")
    assert f"cd {workspace / 'pipelines/pkg'}" in hint
    assert hint.rstrip(")").endswith("tests/test_gate_x.py")
    assert "pipelines/pkg/tests/test_gate_x.py" not in hint


def test_path_helpers_round_trip() -> None:
    assert to_package_path("pipelines/pkg", "pipelines/pkg/tests/t.py") == "tests/t.py"
    assert to_package_path(".", "tests/t.py") == "tests/t.py"
    assert to_repo_node_id("api", "tests/t.py::test_x") == "api/tests/t.py::test_x"
    assert to_repo_node_id("api", "api/tests/t.py::test_x") == "api/tests/t.py::test_x"
    assert to_repo_node_id(".", "tests/t.py::test_x") == "tests/t.py::test_x"


def test_uv_run_prefix_detects_workspace_root(workspace: Path) -> None:
    assert uv_run_prefix(workspace) == ["uv", "run", "--all-packages"]
    assert uv_run_prefix(workspace / "api") == ["uv", "run"]


# ---------------------------------------------------------------------------
# stale local base branch
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_changed_tests_ignore_upstream_commits_missing_from_a_stale_local_main(
    tmp_path: Path,
) -> None:
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(bare), str(work))
    _write(work / "tests/test_base.py", "def test_b():\n    pass\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "base")
    _git(work, "push", "-q", "origin", "HEAD:main")
    # Upstream moves on without the local main knowing.
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(bare), str(other))
    _write(other / "tests/test_upstream.py", "def test_u():\n    pass\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", "upstream")
    _git(other, "push", "-q", "origin", "HEAD:main")
    _git(work, "fetch", "-q", "origin")
    # The PR branch is cut from the fresh origin/main and adds one test.
    wt = prepare_worktree(work, tmp_path / "wt", _git(work, "rev-parse", "origin/main"))
    _write(wt / "tests/test_new.py", "def test_n():\n    pass\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "pr")
    assert _git(work, "rev-parse", "main") != _git(work, "rev-parse", "origin/main")
    assert resolve_diff_base(wt, "main") == "origin/main"
    assert changed_test_files(wt, "main") == ["tests/test_new.py"]


# ---------------------------------------------------------------------------
# slot pool bookkeeping under concurrency
# ---------------------------------------------------------------------------


def _hammer(root: str, n: int) -> None:
    for i in range(n):
        record_size(root, "agent", 8 + (i % 3))


def test_record_size_is_safe_across_processes(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_hammer, args=(str(tmp_path), 150)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    assert [p.exitcode for p in procs] == [0, 0, 0, 0]
    assert declared_size(tmp_path, "agent") in (8, 9, 10)
    assert not list(tmp_path.rglob("pool.*.tmp"))


def test_every_gate_prompt_forbids_pattern_kills() -> None:
    """Regression: a lens ran `pkill -9 -f pytest`, killing every agent whose argv held a prompt."""
    from agent_fleet.gate import prompts

    assert "NEVER kill processes by name or pattern" in prompts.PROCESS_SAFETY
    src = Path(prompts.__file__).read_text(encoding="utf-8")
    assert src.count("return AGENT_RULES + (") == 5


def test_every_gate_prompt_forbids_blocking_commands() -> None:
    """A command that never returns makes the stage a dead agent, not a slow one."""
    from agent_fleet.gate import prompts

    assert "NO BLOCKING COMMANDS" in prompts.NO_BLOCKING_COMMANDS
    assert prompts.NO_BLOCKING_COMMANDS in prompts.AGENT_RULES
    assert prompts.PROCESS_SAFETY in prompts.AGENT_RULES
