"""Baseline-diff failure attribution and persona scoping in CommandVerifier.

The full-pipeline verify path (``CommandVerifier.check``) is the only gate that
runs v0.11.2's checks, yet it used to grade an agent's change against the whole
lane. A lane that was already red on clean main failed every scoped task that
never touched the red files. These tests pin two fixes:

  * ``check`` runs the persona-scoped command set, not the repo-wide one.
  * A failing command is re-run against the base tree (the agent's uncommitted
    edits stashed away). Failures already present without the change are
    attributed as pre-existing and do not block; only newly introduced node-ids
    fail the gate.
"""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

from agent_fleet.contracts.verify_result import VerifyResult, VerifySeverity
from agent_fleet.integrations.command_verifier import CommandVerifier, _parse_failed_ids
from agent_fleet.repo import RepoConfig

if TYPE_CHECKING:
    from pathlib import Path

# A committed "test runner" whose verdict depends on the working tree, so a
# stash-and-rerun genuinely changes its output between head and base.
_RUNNER = (
    "import pathlib, sys\n"
    "names = pathlib.Path('marker.txt').read_text().split()\n"
    "for n in names:\n"
    "    print(f'FAILED tests/test_x.py::{n}')\n"
    "sys.exit(1 if names else 0)\n"
)


def _git_init(tmpdir: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(tmpdir), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(tmpdir),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(tmpdir),
        check=True,
        capture_output=True,
    )


def _commit_all(tmpdir: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=str(tmpdir), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", message], cwd=str(tmpdir), check=True, capture_output=True
    )


def _seed_runner_repo(tmpdir: Path, marker: str) -> RepoConfig:
    _git_init(tmpdir)
    (tmpdir / "fake_tests.py").write_text(_RUNNER, encoding="utf-8")
    (tmpdir / "marker.txt").write_text(marker, encoding="utf-8")
    _commit_all(tmpdir, "seed")
    repo = RepoConfig(repo_root=tmpdir, state_root=tmpdir)
    repo.verify_commands = [f"{sys.executable} fake_tests.py"]
    repo.worktree_bootstrap_commands = []
    repo.critical_path_prefixes = ()
    return repo


def _check(repo: RepoConfig, worktree: Path, persona: str = "coder") -> VerifyResult:
    return CommandVerifier(repo).check(
        worktree, persona=persona, changed_files=[], task_id=1
    )


def test_parse_failed_ids_passing_returns_empty() -> None:
    assert _parse_failed_ids("everything green", "", 0) == frozenset()


def test_parse_failed_ids_extracts_node_ids() -> None:
    out = "FAILED tests/test_x.py::test_a\nFAILED tests/test_y.py::test_b - boom\n"
    assert _parse_failed_ids(out, "", 1) == frozenset(
        {"tests/test_x.py::test_a", "tests/test_y.py::test_b"}
    )


def test_parse_failed_ids_opaque_failure_returns_none() -> None:
    assert _parse_failed_ids("ruff: 3 errors", "", 1) is None


def test_parse_failed_ids_drops_non_python_error_lines() -> None:
    # A bare ERROR log line is not a test node-id and must not be treated as one.
    assert _parse_failed_ids("ERROR could not connect to db", "", 1) is None


def test_preexisting_failure_is_attributed_not_blocked(tmp_path: Path) -> None:
    repo = _seed_runner_repo(tmp_path, "test_a")
    # Agent touches an unrelated file; the lane was already red on test_a.
    (tmp_path / "feature.py").write_text("x = 1\n", encoding="utf-8")

    result = _check(repo, tmp_path)

    assert result.severity is VerifySeverity.OK
    assert any(c.get("attributed_preexisting") for c in result.checks)


def test_introduced_failure_blocks_with_new_node_id(tmp_path: Path) -> None:
    repo = _seed_runner_repo(tmp_path, "test_a")
    # Agent's change makes test_b fail on top of the pre-existing test_a.
    (tmp_path / "marker.txt").write_text("test_a\ntest_b\n", encoding="utf-8")

    result = _check(repo, tmp_path)

    assert result.severity is VerifySeverity.RETRY
    introduced = next(
        ln for ln in result.message.splitlines() if "New failures introduced" in ln
    )
    # The introduced-failures line names only the new node-id, not the pre-existing one.
    assert "tests/test_x.py::test_b" in introduced
    assert "::test_a" not in introduced


def test_opaque_failure_base_green_blocks(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / "keep.txt").write_text("seed\n", encoding="utf-8")
    _commit_all(tmp_path, "seed")
    repo = RepoConfig(repo_root=tmp_path, state_root=tmp_path)
    repo.verify_commands = ["bash -c 'test -f trigger && exit 1 || exit 0'"]
    repo.worktree_bootstrap_commands = []
    repo.critical_path_prefixes = ()
    # Agent introduces the trigger; without it the base tree is green.
    (tmp_path / "trigger").write_text("", encoding="utf-8")

    result = _check(repo, tmp_path)

    assert result.severity is VerifySeverity.RETRY


def test_opaque_failure_base_red_is_attributed(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / "keep.txt").write_text("seed\n", encoding="utf-8")
    _commit_all(tmp_path, "seed")
    repo = RepoConfig(repo_root=tmp_path, state_root=tmp_path)
    repo.verify_commands = ["bash -c 'exit 1'"]
    repo.worktree_bootstrap_commands = []
    repo.critical_path_prefixes = ()
    (tmp_path / "feature.py").write_text("x = 1\n", encoding="utf-8")

    result = _check(repo, tmp_path)

    assert result.severity is VerifySeverity.OK
    assert any(c.get("attributed_preexisting") for c in result.checks)


def test_clean_tree_failure_still_blocks(tmp_path: Path) -> None:
    # No agent edits, so no base can be established; the failure must block.
    repo = _seed_runner_repo(tmp_path, "test_a")

    result = _check(repo, tmp_path)

    assert result.severity is VerifySeverity.RETRY


def test_persona_scoping_runs_lane_command(tmp_path: Path) -> None:
    repo = RepoConfig(repo_root=tmp_path, state_root=tmp_path)
    repo.verify_commands = ["bash -c 'exit 1'"]
    repo.persona_verify_commands = {"lakestore": ("bash -c 'exit 0'",)}
    repo.worktree_bootstrap_commands = []
    repo.critical_path_prefixes = ()

    scoped = _check(repo, tmp_path, persona="lakestore")
    assert scoped.severity is VerifySeverity.OK

    unscoped = _check(repo, tmp_path, persona="gold")
    assert unscoped.severity is VerifySeverity.RETRY
