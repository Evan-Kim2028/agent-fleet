"""Synthesizer JSON-parse retry tests."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from agent_fleet.contracts.research_note import Confidence, ResearchNote
from agent_fleet.contracts.task_spec import (
    DecompositionDecision,
    RiskTier,
    Scope,
    TaskSpec,
)
from agent_fleet.synthesizer import synthesize


@dataclass
class _FakeResult:
    stdout: str
    stderr: str = ""
    exit_code: int = 0
    duration_s: float = 0.0
    agent_id: str | None = None


class _ScriptedBackend:
    """LLMBackend that returns scripted responses, recording each prompt."""

    def __init__(self, outputs: list[str]) -> None:
        self._outputs = list(outputs)
        self.prompts: list[str] = []

    def run(
        self,
        prompt: str,
        *,
        max_tokens: int,
        timeout_s: int,
        memory_limit: str = "4G",
        allowed_tools: list[str] | None = None,
        cwd: object | None = None,
        model: str | None = None,
        mode: object | None = None,
    ) -> _FakeResult:
        self.prompts.append(prompt)
        return _FakeResult(stdout=self._outputs.pop(0))


def _spec() -> TaskSpec:
    return TaskSpec(
        issue_number=4242,
        decomposition_decision=DecompositionDecision.SINGLE,
        decomposition_reason="small",
        child_issues_proposed=[],
        scope=Scope(allowed_paths=["src/"], forbidden_paths=[]),
        research_plan=[],
        acceptance_criteria=["c1"],
        risk_tier=RiskTier.LOW,
        critical_paths_touched=[],
        coordination_spec=None,
    )


def _note() -> ResearchNote:
    return ResearchNote(
        research_id="r1",
        question="q?",
        findings="found",
        scope_paths=["src/"],
        referenced_files=["src/x.py"],
        confidence=Confidence.HIGH,
    )


def _valid_brief_json() -> str:
    return json.dumps(
        {
            "issue_number": 4242,
            "summary": "do the thing",
            "files_to_create": [],
            "files_to_modify": ["src/x.py"],
            "test_strategy": "unit",
            "acceptance_criteria": ["c1"],
            "references": [{"research_id": "r1", "key_finding": "found"}],
            "rollback_plan": None,
        }
    )


def test_synthesize_retries_when_first_output_has_no_json() -> None:
    backend = _ScriptedBackend(
        outputs=[
            "I cannot complete this task.",
            _valid_brief_json(),
        ]
    )
    brief = synthesize(_spec(), [_note()], backend=backend)
    assert brief.issue_number == 4242
    assert len(backend.prompts) == 2
    assert "previous response contained no parseable JSON" in backend.prompts[1]


def test_synthesize_raises_after_two_failed_attempts() -> None:
    backend = _ScriptedBackend(outputs=["no json here", "still no json"])
    with pytest.raises(ValueError, match="after 2 attempts"):
        synthesize(_spec(), [_note()], backend=backend)
    assert len(backend.prompts) == 2
