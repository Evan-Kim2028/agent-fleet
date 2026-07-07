# ruff: noqa: TC002
"""Review phase injects deslop skill text from dispatch equip."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_fleet.config import load_fleet_config
from agent_fleet.hooks import FleetTask
from agent_fleet.level_up.models import DispatchEquip
from agent_fleet.noop_session import NoopLLMResult
from agent_fleet.personas import YamlPersonaResolver
from agent_fleet.phases import run_pipeline

ROOT = Path(__file__).resolve().parent.parent

_REVIEW_JSON = json.dumps(
    {
        "pr_number": 1,
        "verdict": "approve",
        "summary": "looks good",
        "issues": [],
        "shard_id": None,
    }
)


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
            stdout=_REVIEW_JSON,
            stderr="",
            exit_code=0,
            duration_s=0.1,
            agent_id="review-agent",
        )


def test_structured_review_appends_deslop_skill_from_equip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agent_fleet.phases.get_working_tree_diff", lambda _ws: "")

    backend = _CapturingBackend()
    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    resolver = YamlPersonaResolver(fleet_config)
    equip = DispatchEquip(
        persona="reviewer",
        base_loadout="reviewer",
        skill_slots_execute=(),
        skill_slots_review=("pstack/unslop", "cursor-team-kit/deslop"),
        level_up_generation=0,
    )
    task = FleetTask(
        goal="Remove slop from helper",
        persona="coder",
        workspace=str(tmp_path),
        equip=equip,
    )

    run_pipeline(
        backend=backend,  # type: ignore[arg-type]
        resolver=resolver,
        task=task,
        workspace=tmp_path,
        timeout_s=30,
        phases=["review"],
        repo=None,
    )

    assert len(backend.prompts) == 1
    prompt = backend.prompts[0]
    assert "# Review Skills" in prompt
    assert "Unslop" in prompt


def test_legacy_review_appends_deslop_skill_from_equip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _CapturingBackend()
    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    resolver = YamlPersonaResolver(fleet_config)
    equip = DispatchEquip(
        persona="reviewer",
        base_loadout="reviewer",
        skill_slots_execute=(),
        skill_slots_review=("pstack/unslop", "cursor-team-kit/deslop"),
        level_up_generation=0,
    )
    task = FleetTask(goal="Review slop", persona="coder", equip=equip)

    monkeypatch.setattr(
        "agent_fleet.phases.run_structured_review_phase",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("structured path")),
    )
    from agent_fleet.phases import _legacy_review_phase

    _legacy_review_phase(
        backend=backend,  # type: ignore[arg-type]
        resolver=resolver,
        task=task,
        workspace=tmp_path,
        timeout_s=30,
        implementation_summary="Changed helper.py",
        reviewer_persona="reviewer",
        fleet_config=fleet_config,
    )

    assert len(backend.prompts) == 1
    prompt = backend.prompts[0]
    assert "# Review Skills" in prompt
    assert "Unslop" in prompt
