"""Tests for hardened code_review pipeline helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_fleet.contracts.review import ReviewResult, ReviewVerdict
from agent_fleet.phases import resolve_pipeline_outcome, run_scope_phase
from agent_fleet.reviewer import aggregate_verdict
from agent_fleet.scope import files_outside_allowed_paths, path_allowed_by_prefix
from agent_fleet.verify_core import get_working_tree_changes, get_working_tree_diff, is_git_repo


def test_files_outside_allowed_paths() -> None:
    assert files_outside_allowed_paths(("src/",), ["src/a.py", "web/b.ts"]) == ("web/b.ts",)
    assert files_outside_allowed_paths((), ["any/file.py"]) == ()


def test_path_allowed_by_prefix_parent_directory() -> None:
    assert path_allowed_by_prefix("agents/personas/foo.md", "agents/personas/")
    assert path_allowed_by_prefix("agents/", "agents/personas/")
    assert not path_allowed_by_prefix("agents/other/foo.md", "agents/personas/")


def test_files_outside_allowed_paths_directory_entry() -> None:
    allow = ("agents/personas/",)
    assert files_outside_allowed_paths(allow, ["agents/"]) == ()
    assert files_outside_allowed_paths(allow, ["agents/personas/fleet-skills-stack.md"]) == ()
    assert files_outside_allowed_paths(allow, ["agent_fleet/cli.py"]) == ("agent_fleet/cli.py",)


def test_run_scope_phase_passes() -> None:
    from agent_fleet.hooks import Persona

    persona = Persona(
        name="coder",
        prompt_path=Path("coder.md"),
        allowed_tools=[],
        capabilities={},
        allowed_paths=("src/",),
    )
    result = run_scope_phase(
        persona=persona,
        changed_files=["src/a.py"],
    )
    assert result["passed"] is True
    assert result["exit_code"] == 0


def test_run_scope_phase_fails() -> None:
    from agent_fleet.hooks import Persona

    persona = Persona(
        name="coder",
        prompt_path=Path("coder.md"),
        allowed_tools=[],
        capabilities={},
        allowed_paths=("src/",),
    )
    result = run_scope_phase(
        persona=persona,
        changed_files=["infra/x.yaml"],
    )
    assert result["passed"] is False
    assert result["exit_code"] == 1


def test_resolve_pipeline_outcome_scope_violation() -> None:
    status, error = resolve_pipeline_outcome(
        [
            {"phase": "execute", "exit_code": 0},
            {"phase": "scope", "passed": False, "violating_files": ["infra/x.yaml"]},
        ],
        1,
    )
    assert status == "scope_violation"
    assert "infra/x.yaml" in (error or "")


def test_resolve_pipeline_outcome_review_blocked() -> None:
    status, _ = resolve_pipeline_outcome(
        [
            {"phase": "execute", "exit_code": 0},
            {"phase": "scope", "passed": True},
            {"phase": "review", "verdict": ReviewVerdict.BLOCK.value},
        ],
        1,
    )
    assert status == "review_blocked"


def test_aggregate_verdict() -> None:
    reviews = [
        ReviewResult(1, ReviewVerdict.APPROVE, "ok", [], None),
        ReviewResult(1, ReviewVerdict.REQUEST_CHANGES, "nope", [], "src"),
    ]
    assert aggregate_verdict(reviews) == ReviewVerdict.REQUEST_CHANGES


def test_git_helpers_on_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess = pytest.importorskip("subprocess")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello world\n", encoding="utf-8")

    assert is_git_repo(repo)
    assert "README.md" in get_working_tree_changes(repo)
    assert "hello world" in get_working_tree_diff(repo)
