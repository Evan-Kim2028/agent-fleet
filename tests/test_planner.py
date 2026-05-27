"""Tests for the planner phase error-surfacing path."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agent_fleet.cursor_backend import CursorLLMResult
from agent_fleet.planner import plan


class _FakePersonaResolver:
    def list_personas(self) -> list[str]:
        return ["data", "backend", "frontend"]

    def load(self, name: str):  # noqa: ANN202
        raise NotImplementedError


@dataclass
class _FakeSession:
    """LLMSession stub that returns canned CursorLLMResults."""

    result: CursorLLMResult
    sends: list[str] = field(default_factory=list)
    agent_id: str | None = "agent-fake"

    def send(self, prompt: str, **_: object) -> CursorLLMResult:
        self.sends.append(prompt)
        return self.result

    def dispose(self) -> None:
        pass


class _FakeBackend:
    def run(self, *_a: object, **_kw: object) -> CursorLLMResult:
        raise AssertionError("backend.run should not be called when session is provided")


def test_plan_raises_with_diagnostics_on_nonzero_exit_code() -> None:
    """When the LLM call fails (exit_code != 0), plan() must surface the
    backend's stderr instead of masking it as 'no JSON in output'."""
    boom = RuntimeError("cursor SDK auth failure")
    session = _FakeSession(
        result=CursorLLMResult(
            stdout="",
            stderr="RuntimeError: cursor SDK auth failure",
            exit_code=1,
            duration_s=0.1,
            cause=boom,
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
        result=CursorLLMResult(
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
        result=CursorLLMResult(
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
