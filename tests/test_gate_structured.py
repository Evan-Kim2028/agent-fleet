"""Tests for the gate's structured-JSON call machinery."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 - built into concrete paths at runtime
from typing import Any

import pytest

from agent_fleet.gate.structured import (
    StructuredCallError,
    call_structured,
    extract_json_object,
)

# ---------------------------------------------------------------------------
# extract_json_object
# ---------------------------------------------------------------------------


def test_extracts_a_plain_object() -> None:
    assert extract_json_object('{"a": 1}') == {"a": 1}


def test_extracts_from_surrounding_prose() -> None:
    text = 'Here you go:\n{"findings": []}\nHope that helps.'
    assert extract_json_object(text) == {"findings": []}


def test_extracts_from_a_fenced_block() -> None:
    text = 'blah\n```json\n{"verdict": "CONFIRMED"}\n```\n'
    assert extract_json_object(text) == {"verdict": "CONFIRMED"}


def test_prefers_the_first_balanced_object() -> None:
    """Brace-depth walking means a '}' inside a string does not end it early."""
    text = 'noise {"claim": "a } brace", "line": 3} tail'
    assert extract_json_object(text) == {"claim": "a } brace", "line": 3}


def test_handles_nested_objects() -> None:
    text = '{"a": {"b": {"c": [1, 2]}}, "d": 1}'
    assert extract_json_object(text) == {"a": {"b": {"c": [1, 2]}}, "d": 1}


def test_handles_escaped_quotes_and_braces() -> None:
    text = r'{"claim": "he said \"}\" here", "n": 1}'
    assert extract_json_object(text) == {"claim": 'he said "}" here', "n": 1}


def test_ignores_braces_inside_a_longer_string() -> None:
    text = 'x {"a": "}}} not the end", "b": 2} y'
    assert extract_json_object(text) == {"a": "}}} not the end", "b": 2}


def test_prefers_the_first_parsable_fenced_block() -> None:
    text = '```json\nnot json\n```\nsome words\n```json\n{"ok": true}\n```'
    assert extract_json_object(text) == {"ok": True}


def test_raises_when_there_is_no_object() -> None:
    with pytest.raises(ValueError, match="no balanced JSON"):
        extract_json_object("no json here at all")


def test_raises_on_an_unterminated_object() -> None:
    with pytest.raises(ValueError, match="no balanced JSON"):
        extract_json_object('{"a": 1')


# ---------------------------------------------------------------------------
# call_structured
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Result:
    stdout: str
    stderr: str = ""
    exit_code: int = 0
    duration_s: float = 0.0
    agent_id: str | None = None
    usage: dict[str, int] | None = None


class _ScriptedBackend:
    """A backend that replays a scripted list of answers, recording the prompts."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.calls = 0

    def run(self, prompt: str, **_kwargs: Any) -> _Result:  # noqa: ANN401
        self.prompts.append(prompt)
        index = min(self.calls, len(self.answers) - 1)
        self.calls += 1
        return _Result(stdout=self.answers[index])


def _always_valid(_data: dict[str, Any]) -> None:
    """A no-op validator (signature matches contracts.validate_*)."""


def _requires_key(data: dict[str, Any]) -> None:
    if "verdict" not in data:
        raise ValueError("missing verdict")


def test_call_structured_returns_the_parsed_object(tmp_path: Path) -> None:
    backend = _ScriptedBackend(['prose ```json\n{"verdict": "CONFIRMED"}\n```'])
    answer = call_structured(
        backend,  # type: ignore[arg-type]
        "prompt",
        model="m",
        cwd=tmp_path,
        timeout_s=10,
        validate=_requires_key,
    )
    assert answer.data == {"verdict": "CONFIRMED"}
    assert backend.calls == 1


def test_call_structured_retries_once_with_the_error_fed_back(tmp_path: Path) -> None:
    """A malformed answer gets one corrective retry before the role is lost."""
    backend = _ScriptedBackend(["no json here", '{"verdict": "REJECTED"}'])
    answer = call_structured(
        backend,  # type: ignore[arg-type]
        "prompt",
        model="m",
        cwd=tmp_path,
        timeout_s=10,
        validate=_requires_key,
    )
    assert answer.data == {"verdict": "REJECTED"}
    assert backend.calls == 2
    # The retry prompt carries the first failure so the model can correct itself.
    assert "could not be parsed" in backend.prompts[1]


def test_call_structured_raises_after_the_retry_budget(tmp_path: Path) -> None:
    backend = _ScriptedBackend(["still not json"])
    with pytest.raises(StructuredCallError) as exc:
        call_structured(
            backend,  # type: ignore[arg-type]
            "prompt",
            model="m",
            cwd=tmp_path,
            timeout_s=10,
            validate=_requires_key,
        )
    assert "2 attempts" in str(exc.value)
    assert backend.calls == 2


def test_call_structured_rejects_a_schema_violation(tmp_path: Path) -> None:
    backend = _ScriptedBackend(['{"other": 1}'])
    with pytest.raises(StructuredCallError):
        call_structured(
            backend,  # type: ignore[arg-type]
            "prompt",
            model="m",
            cwd=tmp_path,
            timeout_s=10,
            validate=_requires_key,
        )


def test_call_structured_retries_a_backend_failure(tmp_path: Path) -> None:
    class _Failing(_ScriptedBackend):
        def run(self, prompt: str, **_kwargs: Any) -> _Result:  # noqa: ANN401
            self.prompts.append(prompt)
            self.calls += 1
            return _Result(stdout="", stderr="auth expired", exit_code=1)

    backend = _Failing([""])
    with pytest.raises(StructuredCallError, match="auth expired"):
        call_structured(
            backend,
            "prompt",
            model="m",
            cwd=tmp_path,
            timeout_s=10,
            validate=_always_valid,
        )


def test_call_structured_holds_a_slot_while_running(tmp_path: Path) -> None:
    """The machine-wide slot is acquired around the call, not left free."""
    from agent_fleet.slots import SlotPool

    seen: list[bool] = []
    pool = SlotPool("agent", root=tmp_path / "slots", size=1)

    class _Observing(_ScriptedBackend):
        def run(self, _prompt: str, **_kwargs: Any) -> _Result:  # noqa: ANN401
            seen.append(pool.in_use() == 1)
            return _Result(stdout='{"verdict": "CONFIRMED"}')

    call_structured(
        _Observing([""]),
        "prompt",
        model="m",
        cwd=tmp_path,
        timeout_s=10,
        validate=_requires_key,
        slot=pool,
    )
    assert seen == [True]
    assert pool.in_use() == 0


def test_call_structured_respects_max_attempts(tmp_path: Path) -> None:
    backend = _ScriptedBackend(["bad"])
    with pytest.raises(StructuredCallError, match="3 attempts"):
        call_structured(
            backend,  # type: ignore[arg-type]
            "prompt",
            model="m",
            cwd=tmp_path,
            timeout_s=10,
            validate=_requires_key,
            max_attempts=3,
        )
    assert backend.calls == 3
