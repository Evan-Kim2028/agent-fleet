"""Tests for PersonaFoundry: auto-generation of personas on demand."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from agent_fleet.config import FleetConfig
from agent_fleet.persona_foundry import PersonaFoundry, PersonaGenerationError
from agent_fleet.personas import YamlPersonaResolver

_VALID_BODY = """\
## Role

A specialist in data science and machine learning workflows.

## Expertise

Proficient in Python, pandas, scikit-learn, and SQL.
Experienced in feature engineering and model evaluation.

## Scope discipline

Operates within data pipelines and analytical surfaces.
Does not modify production infrastructure or deployment configs.

## Methodology

Iterates hypothesis-driven experiments with reproducible notebooks.
Communicates findings via structured reports with code evidence.
"""


class _FakeResult:
    def __init__(self, text: str) -> None:
        self.stdout = text
        self.stderr = ""
        self.exit_code = 0
        self.duration_s = 0.1
        self.agent_id = None
        self.usage = None


class _StubBackend:
    def __init__(self, response: str = _VALID_BODY) -> None:
        self._response = response
        self.call_count = 0

    def run(self, *_a: object, **_kw: object) -> _FakeResult:
        self.call_count += 1
        return _FakeResult(self._response)


class _ErrorBackend:
    def run(self, *_a: object, **_kw: object) -> _FakeResult:
        raise RuntimeError("backend unavailable")


def _make_foundry(
    tmp_path: Path,
    backend: object = None,
    *,
    response: str = _VALID_BODY,
) -> PersonaFoundry:
    if backend is None:
        backend = _StubBackend(response)
    return PersonaFoundry(
        personas_dir=tmp_path,
        backend=backend,  # type: ignore[arg-type]
        model="test-model",
    )


def _make_resolver(tmp_path: Path) -> YamlPersonaResolver:
    cfg = FleetConfig(personas_dir=tmp_path)
    return YamlPersonaResolver(cfg)


# ---------------------------------------------------------------------------
# Test 1: happy path
# ---------------------------------------------------------------------------


def test_generate_happy_path(tmp_path: Path) -> None:
    foundry = _make_foundry(tmp_path)
    foundry.generate("data-scientist")

    md_path = tmp_path / "data-scientist.md"
    assert md_path.exists(), "expected .md to be written"
    assert md_path.read_text(encoding="utf-8").strip()

    resolver = _make_resolver(tmp_path)
    names = resolver.list_personas()
    assert "data-scientist" in names

    persona = resolver.load("data-scientist")
    assert persona.name == "data-scientist"


# ---------------------------------------------------------------------------
# Test 2: idempotent — pre-existing .md prevents backend call
# ---------------------------------------------------------------------------


def test_generate_idempotent(tmp_path: Path) -> None:
    stub = _StubBackend()
    foundry = PersonaFoundry(personas_dir=tmp_path, backend=stub, model="m")  # type: ignore[arg-type]

    (tmp_path / "analyst.md").write_text(_VALID_BODY, encoding="utf-8")

    foundry.generate("analyst")
    assert stub.call_count == 0, "backend should NOT be called when .md already exists"


# ---------------------------------------------------------------------------
# Test 3: invalid (empty) response raises PersonaGenerationError and no .md
# ---------------------------------------------------------------------------


def test_generate_invalid_response_raises(tmp_path: Path) -> None:
    foundry = _make_foundry(tmp_path, response="")

    with pytest.raises(PersonaGenerationError):
        foundry.generate("bad-persona")

    assert not (tmp_path / "bad-persona.md").exists()


# ---------------------------------------------------------------------------
# Test 4: path-traversal / bad names are rejected
# ---------------------------------------------------------------------------


def test_sanitize_path_traversal(tmp_path: Path) -> None:
    foundry = _make_foundry(tmp_path)

    with pytest.raises(PersonaGenerationError):
        foundry.generate("../../etc/passwd")

    assert not (tmp_path / "../../etc/passwd.md").exists()


def test_sanitize_strips_special_chars(tmp_path: Path) -> None:
    foundry = _make_foundry(tmp_path)
    try:
        foundry.generate("Data Scientist!!")
    except PersonaGenerationError:
        pass
    else:
        # If it succeeded, the resulting file must be within tmp_path
        for f in tmp_path.glob("*.md"):
            assert f.resolve().parent == tmp_path.resolve()


def test_sanitize_backslash_raises(tmp_path: Path) -> None:
    foundry = _make_foundry(tmp_path)
    with pytest.raises(PersonaGenerationError):
        foundry.generate("foo\\bar")


# ---------------------------------------------------------------------------
# Test 5: resolve_or_generate falls back when backend raises
# ---------------------------------------------------------------------------


def test_resolve_or_generate_fallback(tmp_path: Path) -> None:
    foundry = PersonaFoundry(
        personas_dir=tmp_path,
        backend=_ErrorBackend(),  # type: ignore[arg-type]
        model="m",
    )
    result = foundry.resolve_or_generate("x", set(), "coder")
    assert result == "coder"


def test_resolve_or_generate_returns_known(tmp_path: Path) -> None:
    stub = _StubBackend()
    foundry = PersonaFoundry(personas_dir=tmp_path, backend=stub, model="m")  # type: ignore[arg-type]
    result = foundry.resolve_or_generate("coder", {"coder", "reviewer"}, "fallback")
    assert result == "coder"
    assert stub.call_count == 0


def test_resolve_or_generate_generates_unknown(tmp_path: Path) -> None:
    foundry = _make_foundry(tmp_path)
    result = foundry.resolve_or_generate("new-expert", set(), "coder")
    assert result == "new-expert"
    assert (tmp_path / "new-expert.md").exists()


# ---------------------------------------------------------------------------
# Integration tests: foundry wired into child_tasks_from_task_spec
# ---------------------------------------------------------------------------


def _make_task_spec_with_child(persona: str) -> object:
    from agent_fleet.contracts.task_spec import (
        DecompositionDecision,
        RiskTier,
        Scope,
        TaskSpec,
    )

    return TaskSpec(
        issue_number=1,
        decomposition_decision=DecompositionDecision.DECOMPOSE,
        decomposition_reason="test",
        child_issues_proposed=[{"title": "Novel task", "body": "Do the thing", "persona": persona}],
        scope=Scope(allowed_paths=[], forbidden_paths=[]),
        research_plan=[],
        acceptance_criteria=[],
        risk_tier=RiskTier.LOW,
        critical_paths_touched=[],
        coordination_spec=None,
    )


def _make_parent_task() -> object:
    from agent_fleet.hooks import FleetTask

    return FleetTask(
        goal="Parent goal",
        context="",
        persona="coder",
        workspace="/tmp/test-repo",
        pipeline="code_review",
    )


def test_child_tasks_novel_persona_generated(tmp_path: Path) -> None:
    """A novel child persona is synthesized; the returned task carries it."""
    from agent_fleet.config import FleetConfig
    from agent_fleet.orchestration.decompose import child_tasks_from_task_spec
    from agent_fleet.personas import YamlPersonaResolver

    resolver = YamlPersonaResolver(FleetConfig(personas_dir=tmp_path))
    foundry = _make_foundry(tmp_path)
    task_spec = _make_task_spec_with_child("novel-role")

    children = child_tasks_from_task_spec(
        task_spec,  # type: ignore[arg-type]
        parent_task=_make_parent_task(),  # type: ignore[arg-type]
        child_pipeline="code_review",
        persona_resolver=resolver,
        fallback_persona="coder",
        foundry=foundry,
    )

    assert len(children) == 1
    assert children[0].persona == "novel-role"
    assert (tmp_path / "novel-role.md").exists()


def test_child_tasks_foundry_failure_uses_fallback(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When the foundry backend raises, the child task falls back to fallback_persona."""
    import logging

    from agent_fleet.config import FleetConfig
    from agent_fleet.orchestration.decompose import child_tasks_from_task_spec
    from agent_fleet.personas import YamlPersonaResolver

    resolver = YamlPersonaResolver(FleetConfig(personas_dir=tmp_path))
    foundry = PersonaFoundry(
        personas_dir=tmp_path,
        backend=_ErrorBackend(),  # type: ignore[arg-type]
        model="m",
    )
    task_spec = _make_task_spec_with_child("broken-role")

    with caplog.at_level(logging.WARNING, logger="agent_fleet.persona_foundry"):
        children = child_tasks_from_task_spec(
            task_spec,  # type: ignore[arg-type]
            parent_task=_make_parent_task(),  # type: ignore[arg-type]
            child_pipeline="code_review",
            persona_resolver=resolver,
            fallback_persona="coder",
            foundry=foundry,
        )

    assert len(children) == 1
    assert children[0].persona == "coder"
    assert any("broken-role" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Test: generation history
# ---------------------------------------------------------------------------


def test_history_recorded(tmp_path: Path) -> None:
    foundry = _make_foundry(tmp_path)
    foundry.generate("historian")

    registry = tmp_path / ".foundry_history.jsonl"
    assert registry.exists(), ".foundry_history.jsonl must be created"
    lines = [ln for ln in registry.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1

    import json

    row = json.loads(lines[0])
    assert row["name"] == "historian"
    assert row["model"] == "test-model"
    assert row["chars"] > 0

    archive_file = tmp_path / ".foundry_history" / row["archive"]
    assert archive_file.exists(), "archive file must exist under .foundry_history/"
    assert archive_file.read_text(encoding="utf-8") == (tmp_path / "historian.md").read_text(
        encoding="utf-8"
    )


def test_history_appends_per_generation(tmp_path: Path) -> None:
    foundry = _make_foundry(tmp_path)
    foundry.generate("alpha-analyst")
    foundry.generate("beta-engineer")

    registry = tmp_path / ".foundry_history.jsonl"
    assert registry.exists()
    lines = [ln for ln in registry.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 2

    import json

    names = {json.loads(ln)["name"] for ln in lines}
    assert "alpha-analyst" in names
    assert "beta-engineer" in names


def test_history_not_recorded_on_idempotent(tmp_path: Path) -> None:
    stub = _StubBackend()
    foundry = PersonaFoundry(personas_dir=tmp_path, backend=stub, model="m")  # type: ignore[arg-type]

    (tmp_path / "preexisting.md").write_text(_VALID_BODY, encoding="utf-8")
    foundry.generate("preexisting")

    assert stub.call_count == 0
    registry = tmp_path / ".foundry_history.jsonl"
    if registry.exists():
        lines = [ln for ln in registry.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == 0, "no history row should be written on idempotent path"


def test_history_archive_not_in_list_personas(tmp_path: Path) -> None:
    foundry = _make_foundry(tmp_path)
    foundry.generate("archivable-expert")

    resolver = _make_resolver(tmp_path)
    names = resolver.list_personas()

    assert "archivable-expert" in names
    for n in names:
        assert "." not in n or n == "archivable-expert", (
            f"unexpected name with dots in list_personas: {n!r}"
        )
