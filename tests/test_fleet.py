"""Tests for agent_fleet."""

from __future__ import annotations

import argparse
import subprocess
import textwrap
from pathlib import Path
from types import MethodType
from unittest.mock import MagicMock

import pytest

from agent_fleet.backends import make_backend
from agent_fleet.cli import cmd_init
from agent_fleet.config import FleetConfig, load_fleet_config
from agent_fleet.contracts.review import ReviewResult, ReviewVerdict
from agent_fleet.contracts.task_spec import validate_task_spec
from agent_fleet.contracts.tech_lead_review import TechLeadReview, TechLeadVerdict
from agent_fleet.cursor_backend import CursorBackend
from agent_fleet.dispatcher import FleetDispatcher, _normalize_tasks
from agent_fleet.hooks import FleetTask, FleetTaskResult
from agent_fleet.personas import YamlPersonaResolver
from agent_fleet.phase_graph import default_phase_graph
from agent_fleet.repo import RepoConfig, load_repo_config
from agent_fleet.runner import _run_outcome, _spine_from_repo

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def fleet_config() -> FleetConfig:
    return load_fleet_config(ROOT / "fleet.example.yaml")


def test_load_fleet_config(fleet_config: FleetConfig) -> None:
    assert fleet_config.default_model == "composer-2.5"
    assert "coder" in fleet_config.personas
    assert "full" in fleet_config.pipelines


def test_list_personas(fleet_config: FleetConfig) -> None:
    resolver = YamlPersonaResolver(fleet_config)
    names = resolver.list_personas()
    assert "coder" in names
    assert "reviewer" in names


def test_load_persona_prompt(fleet_config: FleetConfig) -> None:
    resolver = YamlPersonaResolver(fleet_config)
    persona = resolver.load("coder")
    assert persona.prompt_path.exists()
    assert persona.allowed_tools


def test_normalize_single_task() -> None:
    tasks, base_branches = _normalize_tasks(
        goal="Fix the bug",
        context="see auth.py",
        persona="coder",
        workspace="/tmp",
        pipeline="simple",
        tasks=None,
    )
    assert len(tasks) == 1
    assert tasks[0].goal == "Fix the bug"
    assert base_branches == [None]


def test_normalize_batch() -> None:
    tasks, base_branches = _normalize_tasks(
        goal=None,
        context=None,
        persona=None,
        workspace=None,
        pipeline=None,
        tasks=[
            {"goal": "A"},
            {"goal": "B", "persona": "explorer", "base_branch": "feature/x"},
        ],
    )
    assert len(tasks) == 2
    assert tasks[1].persona == "explorer"
    assert base_branches == [None, "feature/x"]


def test_normalize_requires_input() -> None:
    with pytest.raises(ValueError):
        _normalize_tasks(
            goal=None,
            context=None,
            persona=None,
            workspace=None,
            pipeline=None,
            tasks=None,
        )


def test_repo_config_example() -> None:
    repo = load_repo_config(ROOT / "examples" / "repo.agent-fleet.yaml")
    assert repo.repo_root == (ROOT / "examples").resolve()
    assert repo.default_persona == "coder"
    assert "pytest -q" in repo.verify_commands


def test_default_phase_graph() -> None:
    graph = default_phase_graph()
    names = [p.name for p in graph]
    assert names[0] == "PLAN"
    assert "IMPLEMENT" in names
    assert "VERIFY" in names


def test_task_spec_schema_minimal() -> None:
    data = {
        "issue_number": 1,
        "decomposition_decision": "single",
        "decomposition_reason": "small change",
        "child_issues_proposed": [],
        "scope": {"allowed_paths": ["src/"], "forbidden_paths": []},
        "research_plan": [
            {
                "id": "r1",
                "question": "Where is auth handled?",
                "scope_paths": ["src/"],
                "needs_browser": False,
            }
        ],
        "acceptance_criteria": ["Tests pass"],
        "risk_tier": "low",
        "critical_paths_touched": [],
        "coordination_spec": None,
    }
    validate_task_spec(data)


def test_task_spec_schema_coordination_spec_accepts_nulls() -> None:
    """Composer commonly emits `null` for unset optional coordination_spec
    fields; the schema must accept that instead of failing PLAN with
    'None is not of type string'."""
    data = {
        "issue_number": 1,
        "decomposition_decision": "single",
        "decomposition_reason": "small change",
        "child_issues_proposed": [],
        "scope": {"allowed_paths": ["src/"], "forbidden_paths": []},
        "research_plan": [],
        "acceptance_criteria": ["Tests pass"],
        "risk_tier": "low",
        "critical_paths_touched": [],
        "coordination_spec": {
            "merge_order": [],
            "schema_contracts_added": [],
            "schema_contracts_removed": [],
            "smoke_test_suggestion": None,
            "shared_branch": None,
            "interface_brief": None,
        },
    }
    validate_task_spec(data)


def test_pipelines_merge_with_defaults(fleet_config: FleetConfig) -> None:
    assert "simple" in fleet_config.pipelines
    assert "code_review" in fleet_config.pipelines
    assert "full" in fleet_config.pipelines


def test_repo_persona_scope_overrides_global(
    fleet_config: FleetConfig,
    tmp_path: Path,
) -> None:
    repo_yaml = tmp_path / ".agent-fleet.yaml"
    repo_yaml.write_text(
        textwrap.dedent(
            """
            name: demo
            persona_scope_allowlist:
              coder:
                - src/
            """
        ),
        encoding="utf-8",
    )
    repo = load_repo_config(repo_yaml)
    fleet_config.repo_config = repo
    persona = YamlPersonaResolver(fleet_config).load("coder")
    assert persona.allowed_paths == ("src/",)


def test_spine_from_repo_applies_cross_cutting_without_scope(tmp_path: Path) -> None:
    repo_yaml = tmp_path / ".agent-fleet.yaml"
    repo_yaml.write_text(
        textwrap.dedent(
            """
            cross_cutting_groups:
              - [frontend/, backend/]
            critical_path_prefixes:
              - .github/workflows/
            """
        ),
        encoding="utf-8",
    )
    repo = load_repo_config(repo_yaml)
    spine = _spine_from_repo(repo)
    assert spine.cross_cutting_groups
    assert ".github/workflows/" in spine.fleet_critical_prefixes


def test_run_outcome_blocks_on_review() -> None:
    reviews = [
        ReviewResult(
            pr_number=1,
            verdict=ReviewVerdict.BLOCK,
            summary="bad",
            issues=[],
            shard_id=None,
        )
    ]
    assert _run_outcome(reviews, None) == "review_blocked"


def test_run_outcome_blocks_on_tech_lead() -> None:
    tech_lead = TechLeadReview(
        pr_number=1,
        verdict=TechLeadVerdict.ESCALATE,
        summary="escalate",
        escalation_required=True,
        disagreement_with_planner=None,
        cross_pr_concerns=[],
    )
    assert _run_outcome([], tech_lead) == "tech_lead_blocked"


def test_init_creates_directory_and_config(tmp_path: Path) -> None:
    target = tmp_path / "new-repo"
    args = argparse.Namespace(path=str(target), force=False)
    assert cmd_init(args) == 0
    assert (target / ".agent-fleet.yaml").exists()


def test_make_backend_cursor_default(fleet_config: FleetConfig) -> None:
    backend = make_backend(fleet_config)
    assert isinstance(backend, CursorBackend)


def test_make_backend_kimi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi-test")
    cfg = load_fleet_config(ROOT / "fleet.example.yaml")
    cfg.default_backend = "kimi"
    backend = make_backend(cfg)
    from agent_fleet.kimi_backend import KimiBackend

    assert isinstance(backend, KimiBackend)
    assert backend.model == "kimi-for-coding"


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True)


def test_dispatcher_keeps_worktree_on_recoverable_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recoverable soft failures with changes should retain the isolated worktree."""
    from agent_fleet.worktree import prepare_task_workspace

    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)
    worktree_base = tmp_path / "worktrees"
    repo_config = RepoConfig(
        repo_root=repo_path,
        use_worktree=True,
        worktree_base=worktree_base,
    )
    shared = prepare_task_workspace(repo_config, task_index=0, force_isolation=True)

    fc = load_fleet_config(ROOT / "fleet.example.yaml")
    fc.default_workspace = str(repo_path)
    dispatcher = FleetDispatcher(config=fc)
    dispatcher.backend = MagicMock()

    monkeypatch.setattr(
        "agent_fleet.dispatcher.find_repo_config",
        lambda _workspace: repo_config,
    )

    def fake_pipeline(**kwargs: object) -> tuple[list[dict[str, object]], str, int, list[str]]:
        del kwargs
        (shared.path / "changed.txt").write_text("edited\n", encoding="utf-8")
        verify_phase: dict[str, object] = {
            "phase": "verify",
            "passed": False,
            "command": "pytest -q",
        }
        return [verify_phase], "verify failed", 1, ["changed.txt"]

    monkeypatch.setattr(
        "agent_fleet.dispatcher.run_configured_pipeline",
        fake_pipeline,
    )

    def prepare_once(**kwargs: object) -> tuple[Path, object, None]:
        del kwargs
        return shared.path, shared, None

    monkeypatch.setattr(
        "agent_fleet.dispatcher.prepare_task_workspace_if_needed",
        prepare_once,
    )

    results = dispatcher.dispatch(
        goal="verify failure retention",
        persona="coder",
        workspace=str(repo_path),
        pipeline="simple",
    )
    assert len(results) == 1
    assert results[0].status == "verify_failed"
    assert results[0].worktree == str(shared.path)
    assert shared.path.exists()
    shared.teardown(keep=False)


def test_parallel_dispatch_warns_on_scope_overlap(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_yaml = tmp_path / ".agent-fleet.yaml"
    repo_yaml.write_text(
        textwrap.dedent(
            """
            name: demo
            persona_scope_allowlist:
              coder:
                - src/
              reviewer:
                - src/
            """
        ),
        encoding="utf-8",
    )
    repo = load_repo_config(repo_yaml)
    fc = load_fleet_config(ROOT / "fleet.example.yaml")
    fc.default_workspace = str(tmp_path)
    fc.max_parallel = 4
    fc.repo_config = repo

    dispatcher = FleetDispatcher(config=fc)
    dispatcher.backend = MagicMock()

    def fake_execute(
        self: FleetDispatcher,
        task_index: int,
        task: FleetTask,
        **kwargs: object,
    ) -> FleetTaskResult:
        del self, kwargs
        return FleetTaskResult(
            task_index=task_index,
            persona=task.persona,
            goal=task.goal,
            status="completed",
            summary=None,
            error=None,
            duration_seconds=0.1,
        )

    monkeypatch.setattr(dispatcher, "_execute_task", MethodType(fake_execute, dispatcher))

    with caplog.at_level("WARNING"):
        results = dispatcher.dispatch(
            tasks=[
                {"goal": "task A", "persona": "coder"},
                {"goal": "task B", "persona": "reviewer"},
            ],
            workspace=str(tmp_path),
            pipeline="simple",
        )

    assert len(results) == 2
    assert any("Parallel batch may collide" in record.message for record in caplog.records)


def test_workstream_subcommand_registered() -> None:
    """agent-fleet workstream must be wired into the top-level CLI."""
    from agent_fleet.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["workstream", "--help"])
    assert exc.value.code == 0
