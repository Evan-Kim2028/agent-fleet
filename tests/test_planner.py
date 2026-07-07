"""Tests for the planner phase error-surfacing path."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from agent_fleet.noop_session import NoopLLMResult
from agent_fleet.planner import plan


class _FakePersonaResolver:
    def list_personas(self) -> list[str]:
        return ["data", "backend", "frontend"]

    def load(self, name: str, *, loadout_size: str | None = None):  # noqa: ANN202
        raise NotImplementedError


@dataclass
class _FakeSession:
    """LLMSession stub that returns canned NoopLLMResults."""

    result: NoopLLMResult
    sends: list[str] = field(default_factory=list)
    agent_id: str | None = "agent-fake"

    def send(self, prompt: str, **_: object) -> NoopLLMResult:
        self.sends.append(prompt)
        return self.result

    def dispose(self) -> None:
        pass


class _FakeBackend:
    def run(self, *_a: object, **_kw: object) -> NoopLLMResult:
        raise AssertionError("backend.run should not be called when session is provided")


def test_plan_raises_with_diagnostics_on_nonzero_exit_code() -> None:
    """When the LLM call fails (exit_code != 0), plan() must surface the
    backend's stderr instead of masking it as 'no JSON in output'."""
    session = _FakeSession(
        result=NoopLLMResult(
            stdout="",
            stderr="RuntimeError: cursor SDK auth failure",
            exit_code=1,
            duration_s=0.1,
        )
    )
    with pytest.raises(ValueError) as excinfo:
        plan(
            issue_number=1,
            issue_title="t",
            issue_body="b",
            backend=_FakeBackend(),  # type: ignore[arg-type]
            persona_resolver=_FakePersonaResolver(),  # type: ignore[arg-type]
            session=session,  # type: ignore[arg-type]
        )

    msg = str(excinfo.value)
    assert "PLAN backend call failed" in msg
    assert "exit_code=1" in msg
    assert "cursor SDK auth failure" in msg
    assert "RuntimeError" in msg
    # Should not retry on backend failure — single send.
    assert len(session.sends) == 1


def test_plan_raises_with_diagnostics_on_empty_stdout() -> None:
    """Empty stdout with exit_code=0 (rare but possible) must still surface as
    a backend-call failure, not the misleading 'no JSON' error."""
    session = _FakeSession(
        result=NoopLLMResult(
            stdout="   \n  ",
            stderr="",
            exit_code=0,
            duration_s=0.1,
        )
    )
    with pytest.raises(ValueError) as excinfo:
        plan(
            issue_number=1,
            issue_title="t",
            issue_body="b",
            backend=_FakeBackend(),  # type: ignore[arg-type]
            persona_resolver=_FakePersonaResolver(),  # type: ignore[arg-type]
            session=session,  # type: ignore[arg-type]
        )
    assert "PLAN backend call failed" in str(excinfo.value)
    assert len(session.sends) == 1


def test_plan_still_retries_on_unparseable_prose() -> None:
    """When the model returns non-JSON prose with exit_code=0, the old retry
    loop must still kick in — that's the original use case."""
    session = _FakeSession(
        result=NoopLLMResult(
            stdout="here is some prose without any json structure",
            stderr="",
            exit_code=0,
            duration_s=0.1,
        )
    )
    with pytest.raises(ValueError) as excinfo:
        plan(
            issue_number=1,
            issue_title="t",
            issue_body="b",
            backend=_FakeBackend(),  # type: ignore[arg-type]
            persona_resolver=_FakePersonaResolver(),  # type: ignore[arg-type]
            session=session,  # type: ignore[arg-type]
            max_retries=2,
        )
    # The old behaviour: retry 3 times (initial + 2 retries) on parse failure.
    assert len(session.sends) == 3
    assert "No JSON object found" in str(excinfo.value)


def test_plan_preserves_program_source_for_program_decision() -> None:
    """Regression: plan() must thread the LLM's ``program`` source into the
    returned TaskSpec. A v0.9.0 oversight dropped it, leaving the program
    dispatch branch (dispatcher gates on ``task_spec.program``) permanently dead
    even though the prompt asks the model to emit it."""
    program_src = "phase('go')\nr = agent('do the thing')\nreturn r.summary"
    spec_json = json.dumps(
        {
            "issue_number": 7,
            "decomposition_decision": "program",
            "decomposition_reason": "dynamic orchestration with runtime fan-out",
            "child_issues_proposed": [],
            "scope": {"allowed_paths": ["src/"], "forbidden_paths": []},
            "research_plan": [],
            "acceptance_criteria": ["the program runs end to end"],
            "risk_tier": "low",
            "critical_paths_touched": [],
            "coordination_spec": None,
            "program": program_src,
        }
    )
    session = _FakeSession(
        result=NoopLLMResult(stdout=spec_json, stderr="", exit_code=0, duration_s=0.1)
    )

    spec = plan(
        issue_number=7,
        issue_title="t",
        issue_body="b",
        backend=_FakeBackend(),  # type: ignore[arg-type]
        persona_resolver=_FakePersonaResolver(),  # type: ignore[arg-type]
        session=session,  # type: ignore[arg-type]
    )

    assert spec.decomposition_decision.value == "program"
    assert spec.program == program_src
