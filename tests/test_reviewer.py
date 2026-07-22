"""Tests for the Reviewer phase's tests-only-changeset guard.

Root cause traced live: a diff containing only `frontend/src/routes/set.test.tsx`
was NOT caught by `is_trivial_pr` (docs/lock/asset patterns don't match test
files) and was NOT an empty changeset (the earlier empty-changeset gate only
fires on zero changed files). It reached the genuine LLM-backed `review()`
call in agent_fleet/reviewer.py, which judged the new test file in isolation
(no signal that the task required an implementation change) and legitimately
returned verdict "approve". `_run_outcome` then mapped that straight to
"completed" with no distinct signal. This guard downgrades a bare APPROVE on
a tests-only changeset to REQUEST_CHANGES with a clear reason, reusing the
existing verdict/outcome vocabulary instead of inventing a new one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_fleet.contracts.review import ReviewVerdict
from agent_fleet.reviewer import (
    TESTS_ONLY_REASON,
    is_tests_only_changeset,
    review,
)

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.agent_mode import AgentMode


@dataclass
class _FakeResult:
    stdout: str
    stderr: str = ""
    exit_code: int = 0
    duration_s: float = 0.1
    agent_id: str | None = None
    usage: dict[str, int] | None = None


class _FakeBackend:
    """Always returns a bare APPROVE, mimicking a genuine-but-naive review."""

    def __init__(self, verdict: str = "approve", issues: list[dict] | None = None) -> None:
        self.verdict = verdict
        self.issues = issues or []
        self.calls = 0

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
        mode: AgentMode | None = None,
    ) -> _FakeResult:
        del prompt, max_tokens, timeout_s, memory_limit, allowed_tools, cwd, model, mode
        self.calls += 1
        payload = {
            "pr_number": 1,
            "verdict": self.verdict,
            "summary": "Looks fine in isolation.",
            "issues": self.issues,
            "shard_id": None,
        }
        return _FakeResult(stdout=json.dumps(payload))


# --- is_tests_only_changeset -------------------------------------------------


def test_is_tests_only_changeset_true_for_pure_test_diff() -> None:
    assert is_tests_only_changeset(["frontend/src/routes/set.test.tsx"])
    assert is_tests_only_changeset(["agent_fleet/foo_test.py"])
    assert is_tests_only_changeset(["tests/test_bar.py"])
    assert is_tests_only_changeset(["src/__tests__/thing.spec.ts"])


def test_is_tests_only_changeset_false_for_empty() -> None:
    # Empty changeset is the OTHER gate's job (empty-changeset), not this one.
    assert is_tests_only_changeset([]) is False


def test_is_tests_only_changeset_false_when_any_non_test_file_present() -> None:
    assert not is_tests_only_changeset(
        ["frontend/src/routes/set.tsx", "frontend/src/routes/set.test.tsx"]
    )
    assert not is_tests_only_changeset(["agent_fleet/reviewer.py"])


# --- review() guard integration ---------------------------------------------


def test_tests_only_changeset_does_not_yield_bare_approve() -> None:
    backend = _FakeBackend(verdict="approve")
    results = review(
        1,
        "diff --git a/frontend/src/routes/set.test.tsx ...",
        ["frontend/src/routes/set.test.tsx"],
        backend=backend,
        max_tokens=100,
        timeout_s=10,
    )
    assert len(results) == 1
    result = results[0]
    assert result.verdict == ReviewVerdict.REQUEST_CHANGES
    assert TESTS_ONLY_REASON in result.summary
    assert any(TESTS_ONLY_REASON in str(issue.get("message", "")) for issue in result.issues)


def test_tests_only_changeset_guard_is_overridable() -> None:
    backend = _FakeBackend(verdict="approve")
    results = review(
        1,
        "diff --git a/tests/test_regression.py ...",
        ["tests/test_regression.py"],
        backend=backend,
        max_tokens=100,
        timeout_s=10,
        allow_tests_only_approval=True,
    )
    assert results[0].verdict == ReviewVerdict.APPROVE


def test_mixed_changeset_unaffected_and_can_approve() -> None:
    backend = _FakeBackend(verdict="approve")
    results = review(
        1,
        "diff --git a/agent_fleet/routes.py ... b/tests/test_routes.py ...",
        ["agent_fleet/routes.py", "tests/test_routes.py"],
        backend=backend,
        max_tokens=100,
        timeout_s=10,
    )
    assert results[0].verdict == ReviewVerdict.APPROVE
    assert TESTS_ONLY_REASON not in results[0].summary


def test_tests_only_changeset_does_not_touch_non_approve_verdicts() -> None:
    backend = _FakeBackend(verdict="block")
    results = review(
        1,
        "diff --git a/tests/test_regression.py ...",
        ["tests/test_regression.py"],
        backend=backend,
        max_tokens=100,
        timeout_s=10,
    )
    # Already BLOCK — guard should not relabel or weaken it.
    assert results[0].verdict == ReviewVerdict.BLOCK


# --- goal/context threading (root-cause fix for reviewer blind to task) -----


@dataclass
class _PromptCapturingBackend:
    """Fake backend that records the prompt it received and returns a fixed verdict."""

    verdict: str = "approve"
    prompts: list[str] | None = None

    def __post_init__(self) -> None:
        if self.prompts is None:
            self.prompts = []

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
        mode: AgentMode | None = None,
    ) -> _FakeResult:
        del max_tokens, timeout_s, memory_limit, allowed_tools, cwd, model, mode
        self.prompts.append(prompt)
        payload = {
            "pr_number": 1,
            "verdict": self.verdict,
            "summary": "stub summary",
            "issues": [],
            "shard_id": None,
        }
        return _FakeResult(stdout=json.dumps(payload))


def test_review_prompt_contains_task_goal_when_supplied() -> None:
    backend = _PromptCapturingBackend(verdict="approve")
    review(
        1,
        "diff --git a/agent_fleet/routes.py ...",
        ["agent_fleet/routes.py"],
        backend=backend,
        max_tokens=100,
        timeout_s=10,
        task_goal="Fix the P0 HTTP 500 bug in the routes handler",
        task_context="Reported by on-call; endpoint /v1/foo crashes on empty body",
        implementation_summary="Added null check before dereferencing body",
    )
    assert backend.prompts is not None
    assert len(backend.prompts) == 1
    prompt = backend.prompts[0]
    assert "Fix the P0 HTTP 500 bug in the routes handler" in prompt
    assert "Reported by on-call" in prompt
    assert "Added null check before dereferencing body" in prompt
    # Prompt explicitly instructs the model to flag off-task/incomplete diffs.
    assert "off-task" in prompt.lower() or "accomplish" in prompt.lower()


def test_review_behavior_unchanged_when_goal_and_context_none() -> None:
    """Back-compat: callers that don't pass goal/context see the same prompt shape."""
    backend = _PromptCapturingBackend(verdict="approve")
    results = review(
        1,
        "diff --git a/agent_fleet/routes.py ...",
        ["agent_fleet/routes.py"],
        backend=backend,
        max_tokens=100,
        timeout_s=10,
    )
    assert results[0].verdict == ReviewVerdict.APPROVE
    assert backend.prompts is not None
    prompt = backend.prompts[0]
    assert "Original task" not in prompt
    assert "Task context" not in prompt
    assert "Implementer summary" not in prompt
    # No goal supplied → no goal-check instruction injected either.
    assert "off-task" not in prompt.lower()


def test_off_task_diff_surfaced_as_request_changes_not_approve() -> None:
    """A diff that plainly does not address the stated goal must not be a bare APPROVE.

    Simulates the reviewer LLM correctly following the goal-check instruction:
    task asked for a bug fix, diff only adds an unrelated test file, so the
    (stubbed) backend returns request_changes instead of approve.
    """
    backend = _PromptCapturingBackend(verdict="request_changes")
    results = review(
        1,
        "diff --git a/tests/test_unrelated.py ...",
        ["tests/test_unrelated.py"],
        backend=backend,
        max_tokens=100,
        timeout_s=10,
        task_goal="Fix the P0 HTTP 500 bug in the routes handler",
        implementation_summary="Added a new unrelated test file; did not touch routes.py",
    )
    assert results[0].verdict == ReviewVerdict.REQUEST_CHANGES
    assert results[0].verdict != ReviewVerdict.APPROVE
