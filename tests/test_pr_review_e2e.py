"""End-to-end wiring tests for PR analyzer (mock backend, no live LLM)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_fleet.pr_review.config import load_pr_review_config
from agent_fleet.pr_review.prompts import build_prompt
from agent_fleet.pr_review.runner import run_pr_review
from agent_fleet.repo import find_repo_config, load_repo_config


@dataclass
class _MockResult:
    stdout: str
    stderr: str = ""
    exit_code: int = 0
    duration_s: float = 0.1
    agent_id: str | None = None


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
        mode: str = "agent",
    ) -> _MockResult:
        del prompt, max_tokens, timeout_s, memory_limit, allowed_tools, cwd, model, mode
        body = self.responses.pop(0) if self.responses else "{}"
        return _MockResult(stdout=body)


_MOCK_ANALYSIS = json.dumps(
    {
        "pr_type": "backend",
        "primary_areas": ["lakestore"],
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


def test_lake_of_rage_pr_review_config_loads() -> None:
    repo = load_repo_config("/home/evan/Documents/lake-of-rage/.agent-fleet.yaml")
    assert repo.pr_review is not None
    assert repo.pr_review.enabled is True
    assert repo.pr_review.use_in_code_review is True
    assert repo.pr_review.overlay_path is not None
    assert repo.pr_review.overlay_path.name == "pr_review_overlay.md"


def test_lake_of_rage_overlay_in_prompt() -> None:
    repo = find_repo_config("/home/evan/Documents/lake-of-rage")
    assert repo is not None and repo.pr_review is not None
    prompt = build_prompt(
        "diff --git a/x.py b/x.py\n+pass\n",
        ["packages/lakestore/x.py"],
        "backend-security",
        repo.pr_review,
    )
    assert "Repository-specific invariants" in prompt
    assert "solana_client.py" in prompt


def test_run_pr_review_mock_backend_on_lake_of_rage() -> None:
    backend = _MockBackend(responses=[_MOCK_ANALYSIS, _MOCK_ANALYSIS])
    result = run_pr_review(
        workspace=Path("/home/evan/Documents/lake-of-rage"),
        backend=backend,
        base_branch="main",
    )
    assert result["verdict"] == "approve"
    assert "Composer PR Analysis" in str(result["comment_markdown"])
    assert isinstance(result["changed_files"], list)


def test_hermes_pr_review_schema_registered() -> None:
    from agent_fleet.integrations.hermes import schemas

    assert schemas.CODING_FLEET_PR_REVIEW["name"] == "coding_fleet_pr_review"
    assert "workspace" in schemas.CODING_FLEET_PR_REVIEW["parameters"]["properties"]
