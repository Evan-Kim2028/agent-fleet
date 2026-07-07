# ruff: noqa: TC002
"""Code review auto-fix phase uses dispatch equip compose body."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_fleet.code_review.fix import run_fix_phase
from agent_fleet.config import load_fleet_config
from agent_fleet.hooks import FleetTask
from agent_fleet.level_up.models import DispatchEquip
from agent_fleet.level_up.paths import persona_dir
from agent_fleet.noop_session import NoopLLMResult
from agent_fleet.personas import YamlPersonaResolver
from agent_fleet.repo import load_repo_config

ROOT = Path(__file__).resolve().parent.parent


class _CapturingBackend:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def run(
        self,
        prompt: str,
        *,
        max_tokens: int,
        timeout_s: int,
        memory_limit: str = "4G",
        allowed_tools: list[str] | None = None,
        cwd: Path | None = None,
        model: str | None = None,
        mode: object | None = None,
    ) -> NoopLLMResult:
        del max_tokens, timeout_s, memory_limit, allowed_tools, cwd, model, mode
        self.prompts.append(prompt)
        return NoopLLMResult(
            stdout="fixed",
            stderr="",
            exit_code=0,
            duration_s=0.1,
            agent_id="fix-agent",
        )


def _patch_level_up_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr("agent_fleet.level_up.paths.LEVEL_UP_ROOT", root)


def test_fix_phase_uses_task_equip_fast_path(
    tmp_path: Path,
) -> None:
    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    resolver = YamlPersonaResolver(fleet_config)
    backend = _CapturingBackend()
    equip = DispatchEquip(
        persona="coder",
        base_loadout="coder",
        skill_slots_execute=("pstack/tdd",),
        skill_slots_review=(),
        level_up_generation=0,
        compose_body="# Equip compose\n\nTDD Bug Fix",
    )
    task = FleetTask(
        goal="Implement widget",
        context="",
        persona="coder",
        workspace=str(tmp_path),
        equip=equip,
    )

    with patch("agent_fleet.code_review.fix.resolve_dispatch_equip") as mock_resolve:
        run_fix_phase(
            backend=backend,
            resolver=resolver,
            task=task,
            workspace=tmp_path,
            timeout_s=60,
            phase_results=[{"phase": "review", "verdict": "request_changes"}],
            repo=None,
            fix_persona="coder",
            attempt=1,
        )

    mock_resolve.assert_not_called()
    assert len(backend.prompts) == 1
    prompt = backend.prompts[0]
    assert "TDD Bug Fix" in prompt
    assert "# Review feedback" in prompt
    assert "# Verify failures\n(none)" in prompt


def test_fix_phase_resolves_equip_when_fix_persona_differs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_level_up_root(monkeypatch, tmp_path / "level_up")
    repo_yaml = tmp_path / ".agent-fleet.yaml"
    repo_yaml.write_text("name: fix-persona-mismatch\n", encoding="utf-8")
    repo = load_repo_config(repo_yaml)
    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    resolver = YamlPersonaResolver(fleet_config)
    backend = _CapturingBackend()
    execute_equip = DispatchEquip(
        persona="coder",
        base_loadout="coder",
        skill_slots_execute=("pstack/tdd",),
        skill_slots_review=(),
        level_up_generation=0,
        compose_body="# Execute equip\n\nExecute-only body",
        parent_run_id="parent-run-1",
    )
    task = FleetTask(
        goal="Implement widget",
        context="ctx",
        persona="coder",
        workspace=str(tmp_path),
        equip=execute_equip,
    )

    run_fix_phase(
        backend=backend,
        resolver=resolver,
        task=task,
        workspace=tmp_path,
        timeout_s=60,
        phase_results=[],
        repo=repo,
        fix_persona="reviewer",
        attempt=2,
    )

    prompt = backend.prompts[0]
    assert "Execute-only body" not in prompt
    assert "# Persona" in prompt
    assert "# Review feedback\n(none)" in prompt
    assert "# Verify failures\n(none)" in prompt

    journal_path = persona_dir("fix-persona-mismatch", "reviewer") / "journal.jsonl"
    assert journal_path.is_file()
    rows = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    assert any(row.get("run_id") == "parent-run-1-fix-2" for row in rows)


def test_fix_phase_journals_run_id_without_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_level_up_root(monkeypatch, tmp_path / "level_up")
    repo_yaml = tmp_path / ".agent-fleet.yaml"
    repo_yaml.write_text("name: fix-run-id\n", encoding="utf-8")
    repo = load_repo_config(repo_yaml)
    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    resolver = YamlPersonaResolver(fleet_config)
    backend = _CapturingBackend()
    task = FleetTask(goal="Fix tests", persona="coder", workspace=str(tmp_path))

    with patch("agent_fleet.code_review.fix.resolve_dispatch_equip") as mock_resolve:
        mock_resolve.return_value = DispatchEquip(
            persona="coder",
            base_loadout="coder",
            skill_slots_execute=(),
            skill_slots_review=(),
            level_up_generation=0,
            compose_body="Coder body",
        )
        run_fix_phase(
            backend=backend,
            resolver=resolver,
            task=task,
            workspace=tmp_path,
            timeout_s=60,
            phase_results=[],
            repo=repo,
            fix_persona="reviewer",
            attempt=3,
        )

    mock_resolve.assert_called_once()
    assert mock_resolve.call_args.kwargs["run_id"] == "code-review-fix-3"
