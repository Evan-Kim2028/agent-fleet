# ruff: noqa: TC002
"""Skill lifecycle integration tests (equip compose_body → implementer)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_fleet.config import load_fleet_config
from agent_fleet.contracts.implementation_brief import ImplementationBrief
from agent_fleet.contracts.task_spec import (
    DecompositionDecision,
    RiskTier,
    Scope,
    TaskSpec,
)
from agent_fleet.hooks import FleetTask, Persona
from agent_fleet.implementer import implement
from agent_fleet.orchestration.equip import resolve_dispatch_equip
from agent_fleet.personas import YamlPersonaResolver
from agent_fleet.repo import load_repo_config

ROOT = Path(__file__).resolve().parent.parent


def _patch_level_up_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr("agent_fleet.level_up.paths.LEVEL_UP_ROOT", root)


@dataclass
class _FakeResult:
    stdout: str
    stderr: str = ""
    exit_code: int = 0
    duration_s: float = 0.0
    agent_id: str | None = None


class _CapturingBackend:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def run(
        self,
        prompt: str,
        *,
        max_tokens: int,  # noqa: ARG002
        timeout_s: int,  # noqa: ARG002
        memory_limit: str = "4G",  # noqa: ARG002
        allowed_tools: list[str] | None = None,  # noqa: ARG002
        cwd: Path | None = None,  # noqa: ARG002
        model: str | None = None,  # noqa: ARG002
        mode: object | None = None,  # noqa: ARG002
    ) -> _FakeResult:
        self.prompts.append(prompt)
        return _FakeResult(stdout="ok")


class _StubPersonaResolver:
    def __init__(self, persona: Persona) -> None:
        self._persona = persona

    def load(self, name: str) -> Persona:  # noqa: ARG002
        return self._persona

    def list_personas(self) -> list[str]:
        return [self._persona.name]


def _brief() -> ImplementationBrief:
    return ImplementationBrief(
        issue_number=1,
        summary="Fix bug",
        files_to_create=[],
        files_to_modify=["src/x.py"],
        test_strategy="pytest",
        acceptance_criteria=["tests pass"],
        references=[],
    )


def _task_spec() -> TaskSpec:
    return TaskSpec(
        issue_number=1,
        decomposition_decision=DecompositionDecision.SINGLE,
        decomposition_reason="small",
        child_issues_proposed=[],
        scope=Scope(allowed_paths=["src/"], forbidden_paths=[]),
        research_plan=[],
        acceptance_criteria=["tests pass"],
        risk_tier=RiskTier.LOW,
        critical_paths_touched=[],
        coordination_spec=None,
    )


def test_resolve_dispatch_equip_compose_body_includes_tdd_for_coder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_level_up_root(monkeypatch, tmp_path / "level_up")

    repo_yaml = tmp_path / ".agent-fleet.yaml"
    repo_yaml.write_text("name: skill-lifecycle\n", encoding="utf-8")
    repo = load_repo_config(repo_yaml)
    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    task = FleetTask(goal="Fix bug", persona="coder", workspace=str(tmp_path))

    equip = resolve_dispatch_equip(task, fleet_config, repo, run_id="run-sl")

    assert "pstack/tdd" in equip.skill_slots_execute
    assert "TDD Bug Fix" in equip.compose_body


def test_implement_accepts_compose_body_override(tmp_path: Path) -> None:
    persona = Persona(
        name="coder",
        prompt_path=Path("/nonexistent/coder.md"),
        allowed_tools=[],
        capabilities={},
    )
    backend = _CapturingBackend()
    compose_body = "# Equip compose\n\nTDD Bug Fix\n\nWrite failing test first."

    implement(
        _brief(),
        _task_spec(),
        tmp_path,
        "fleet/test-branch",
        backend=backend,
        persona_resolver=_StubPersonaResolver(persona),
        persona_name="coder",
        compose_body=compose_body,
    )

    assert len(backend.prompts) == 1
    prompt = backend.prompts[0]
    assert "TDD Bug Fix" in prompt
    assert "You are a helpful coding assistant." not in prompt


def test_implement_falls_back_when_compose_body_empty(tmp_path: Path) -> None:
    prompt_path = tmp_path / "coder.md"
    prompt_path.write_text("Static markdown persona body.", encoding="utf-8")
    persona = Persona(
        name="coder",
        prompt_path=prompt_path,
        allowed_tools=[],
        capabilities={},
    )
    backend = _CapturingBackend()

    implement(
        _brief(),
        _task_spec(),
        tmp_path,
        "fleet/test-branch",
        backend=backend,
        persona_resolver=_StubPersonaResolver(persona),
        persona_name="coder",
        compose_body="",
    )

    assert "Static markdown persona body." in backend.prompts[0]
    assert "TDD Bug Fix" not in backend.prompts[0]


def test_legacy_review_uses_reviewer_loadout_compose_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agent_fleet.level_up.paths.LEVEL_UP_ROOT", tmp_path / "level_up")

    backend = _CapturingBackend()
    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    resolver = YamlPersonaResolver(fleet_config)
    task = FleetTask(goal="Review changes", persona="coder", workspace=str(tmp_path))

    from agent_fleet.phases import _legacy_review_phase

    _legacy_review_phase(
        backend=backend,  # type: ignore[arg-type]
        resolver=resolver,
        task=task,
        workspace=tmp_path,
        timeout_s=30,
        implementation_summary="Updated module",
        reviewer_persona="reviewer",
        fleet_config=fleet_config,
    )

    assert len(backend.prompts) == 1
    prompt = backend.prompts[0]
    assert "Remove AI code slop" in prompt or "deslop" in prompt.lower()


def test_fix_phase_injects_equip_compose_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agent_fleet.level_up.paths.LEVEL_UP_ROOT", tmp_path / "level_up")

    captured: list[str] = []

    class _FixBackend:
        def run(self, prompt: str, **kwargs: object) -> _FakeResult:  # noqa: ARG002
            captured.append(prompt)
            return _FakeResult(stdout="fixed")

    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    resolver = YamlPersonaResolver(fleet_config)
    task = FleetTask(goal="Fix CI", persona="coder", workspace=str(tmp_path))

    from agent_fleet.code_review.fix import run_fix_phase

    run_fix_phase(
        backend=_FixBackend(),  # type: ignore[arg-type]
        resolver=resolver,
        task=task,
        workspace=tmp_path,
        timeout_s=30,
        phase_results=[{"phase": "review", "verdict": "request_changes", "stdout": "fix tests"}],
        repo=None,
        fix_persona="coder",
        attempt=1,
        fleet_config=fleet_config,
    )

    assert len(captured) == 1
    assert "# Persona" in captured[0]
    assert "pstack/tdd" in captured[0].lower() or "tdd" in captured[0].lower()
