"""End-to-end wiring tests for PR analyzer (mock backend, no live LLM)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from agent_fleet.pr_review.prompts import build_prompt
from agent_fleet.pr_review.runner import run_pr_review
from agent_fleet.repo import find_repo_config, load_repo_config

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.agent_mode import AgentMode


@dataclass
class _MockResult:
    stdout: str
    stderr: str = ""
    exit_code: int = 0
    duration_s: float = 0.1
    agent_id: str | None = None
    usage: dict[str, int] | None = None


@dataclass
class _MockBackend:
    responses: list[str]

    def run(
        self,
        prompt: str,
        *,
        max_tokens: int = 0,
        timeout_s: int = 720,
        memory_limit: str = "2G",
        allowed_tools: list[str] | None = None,
        cwd: Path | None = None,
        model: str | None = None,
        mode: AgentMode | None = None,
    ) -> _MockResult:
        del prompt, max_tokens, timeout_s, memory_limit, allowed_tools, cwd, model, mode
        body = self.responses.pop(0) if self.responses else "{}"
        return _MockResult(stdout=body)


_MOCK_ANALYSIS = json.dumps(
    {
        "pr_type": "backend",
        "primary_areas": ["backend"],
        "risk_level": "low",
        "risk_reasoning": "Mock analysis passed.",
        "summary": "Test change looks fine.",
        "deep_analysis": "No issues in mock run.",
        "recommendations": {"backend_check": True, "security_check": True},
        "methodology_checklist": {
            "integration_tests_present": True,
            "integration_tests_detail": "mock",
            "error_paths_tested": True,
            "error_paths_detail": "mock",
            "cross_system_contracts_verified": True,
            "cross_system_detail": "mock",
            "debug_code_removed": True,
            "debug_code_detail": "mock",
            "type_checking_verified": True,
            "type_checking_detail": "mock",
        },
        "findings": [],
        "suggestions": [],
    }
)


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "sample-repo"
    repo.mkdir()
    overlay_dir = repo / "agents"
    overlay_dir.mkdir()
    (overlay_dir / "pr_review_overlay.md").write_text(
        "## Repository-specific invariants\n\nAlways validate user input.\n",
        encoding="utf-8",
    )
    (repo / ".agent-fleet.yaml").write_text(
        """\
name: sample-app
default_branch: main
pr_review:
  enabled: true
  use_in_code_review: true
  overlay: agents/pr_review_overlay.md
""",
        encoding="utf-8",
    )
    return repo


def test_pr_review_config_loads(sample_repo: Path) -> None:
    repo = load_repo_config(sample_repo / ".agent-fleet.yaml")
    assert repo.pr_review is not None
    assert repo.pr_review.enabled is True
    assert repo.pr_review.use_in_code_review is True
    assert repo.pr_review.overlay_path is not None
    assert repo.pr_review.overlay_path.name == "pr_review_overlay.md"


def test_overlay_in_prompt(sample_repo: Path) -> None:
    repo = find_repo_config(sample_repo)
    assert repo is not None and repo.pr_review is not None
    prompt = build_prompt(
        "diff --git a/src/app.py b/src/app.py\n+pass\n",
        ["src/app.py"],
        "backend-security",
        repo.pr_review,
    )
    assert "Repository-specific invariants" in prompt
    assert "Always validate user input" in prompt


@patch("agent_fleet.pr_review.runner.get_working_tree_diff")
def test_run_pr_review_mock_backend(
    mock_diff: MagicMock,
    sample_repo: Path,
) -> None:
    mock_diff.return_value = (
        "diff --git a/src/app.py b/src/app.py\n+pass\n",
        ["src/app.py"],
    )
    backend = _MockBackend(responses=[_MOCK_ANALYSIS, _MOCK_ANALYSIS])
    result = run_pr_review(
        workspace=sample_repo,
        backend=backend,
        base_branch="main",
    )
    assert result["verdict"] == "approve"
    assert "Composer PR Analysis" in str(result["comment_markdown"])
    assert isinstance(result["changed_files"], list)


def test_hermes_pr_review_schema_registered() -> None:
    from typing import Any, cast

    from integrations.hermes import schemas

    schema = cast("dict[str, Any]", schemas.CODING_FLEET_PR_REVIEW)
    assert schema["name"] == "coding_fleet_pr_review"
    params = cast("dict[str, Any]", schema["parameters"])
    assert "workspace" in params["properties"]
