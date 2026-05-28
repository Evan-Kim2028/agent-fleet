"""Tests for hardened code_review pipeline helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from agent_fleet.contracts.review import ReviewResult, ReviewVerdict
from agent_fleet.phases import resolve_pipeline_outcome, run_scope_phase
from agent_fleet.reviewer import aggregate_verdict
from agent_fleet.scope import (
    effective_allowed_paths,
    files_outside_allowed_paths,
    path_allowed_by_prefix,
)
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


def test_effective_allowed_paths_task_wins_over_persona() -> None:
    assert effective_allowed_paths(("tests/",), ("src/",)) == ("tests/",)
    assert effective_allowed_paths((), ("src/",)) == ("src/",)
    assert effective_allowed_paths(("tests/",), ()) == ("tests/",)
    assert effective_allowed_paths((), ()) == ()


def test_run_scope_phase_task_scope_overrides_unrestricted_persona() -> None:
    from agent_fleet.hooks import FleetTask, Persona

    persona = Persona(
        name="lakestore",
        prompt_path=Path("lakestore.md"),
        allowed_tools=[],
        capabilities={},
        allowed_paths=(),
    )
    task = FleetTask(
        goal="tripwire",
        persona="lakestore",
        allowed_paths=("tests/", "packages/lakestore/"),
    )
    result = run_scope_phase(
        persona=persona,
        changed_files=["pipelines/pokemontcg_pipe/src/pipe/gold/build_sales.py"],
        task=task,
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


class _StubPersona:
    name = "coder"
    allowed_paths: tuple[str, ...] = ()


class _StubResolver:
    def load(self, _name: str) -> _StubPersona:
        return _StubPersona()


def _run_review_pipeline(monkeypatch: pytest.MonkeyPatch, *, review_blocking: bool) -> int:
    """Drive run_pipeline with a passing execute/scope and a REQUEST_CHANGES review."""
    import agent_fleet.phases as ph
    from agent_fleet.hooks import FleetTask

    monkeypatch.setattr(
        ph, "run_execute_phase", lambda **_k: {"phase": "execute", "stdout": "", "exit_code": 0}
    )
    monkeypatch.setattr(ph, "collect_changed_files", lambda _ws: [])
    monkeypatch.setattr(
        ph,
        "run_structured_review_phase",
        lambda **_k: {
            "phase": "review",
            "verdict": ReviewVerdict.REQUEST_CHANGES.value,
            "summary": "speculative concern",
            "exit_code": 1,
            "passed": False,
        },
    )

    _results, _summary, exit_code, _changed = ph.run_pipeline(
        backend=cast("Any", object()),
        resolver=cast("Any", _StubResolver()),
        task=FleetTask(goal="x", persona="coder"),
        workspace=Path("/tmp"),
        timeout_s=10,
        phases=["execute", "review"],
        repo=None,
        review_blocking=review_blocking,
    )
    return exit_code


def test_review_advisory_does_not_block_green_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run-8 guard: a REQUEST_CHANGES review must not red an otherwise-green pipeline."""
    assert _run_review_pipeline(monkeypatch, review_blocking=False) == 0


def test_review_blocking_opt_in_still_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run_review_pipeline(monkeypatch, review_blocking=True) == 1


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
