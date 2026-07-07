"""Regression tests for pr_loop lane invariants.

Test A: preflight command resolution honours persona_verify_commands.
Test B: fix-prompt construction passes allowed_paths to build_agent_prompt.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from agent_fleet.config import load_fleet_config
from agent_fleet.hooks import Persona
from agent_fleet.noop_session import NoopLLMResult
from agent_fleet.pr_loop.config import PrLoopConfig
from agent_fleet.pr_loop.lifecycle import (
    _commit_preflight_commands,
    address_review_findings,
    attempt_ci_fix,
    persona_from_branch,
)
from agent_fleet.repo import RepoConfig

if TYPE_CHECKING:
    import pytest

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _repo_with_persona_verify(
    tmp_path: Path,
    *,
    persona: str = "lakestore",
    persona_cmds: list[str] | None = None,
    global_cmds: list[str] | None = None,
    persona_allowed_paths: tuple[str, ...] = (),
) -> RepoConfig:
    persona_cmds = persona_cmds or [f"ruff check packages/{persona}"]
    global_cmds = global_cmds or ["pytest", "ruff check ."]
    repo = RepoConfig(
        repo_root=tmp_path,
        default_persona="coder",
        verify_commands=global_cmds,
        persona_verify_commands={persona: tuple(persona_cmds)},
        persona_scope_allowlist=(
            {persona: (f"packages/{persona}/",)} if persona_allowed_paths else {}
        ),
    )
    if persona_allowed_paths:
        repo = RepoConfig(
            repo_root=tmp_path,
            default_persona="coder",
            verify_commands=global_cmds,
            persona_verify_commands={persona: tuple(persona_cmds)},
            persona_scope_allowlist={persona: persona_allowed_paths},
        )
    return repo


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
            stdout="done",
            stderr="",
            exit_code=0,
            duration_s=0.1,
            agent_id="fix-agent",
        )


def _patch_level_up_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr("agent_fleet.level_up.paths.LEVEL_UP_ROOT", root)


# ---------------------------------------------------------------------------
# Test A — persona-scoped preflight command resolution
# ---------------------------------------------------------------------------


def test_commit_preflight_uses_persona_verify_commands(tmp_path: Path) -> None:
    """_commit_preflight_commands must return persona-scoped list, not global."""
    repo = _repo_with_persona_verify(
        tmp_path,
        persona="lakestore",
        persona_cmds=["ruff check packages/lakestore"],
        global_cmds=["pytest", "ruff check ."],
    )
    persona_name = persona_from_branch("fleet/lakestore/42-issue", repo.default_persona)
    assert persona_name == "lakestore"

    cmds = _commit_preflight_commands(repo, persona_name)

    assert cmds == ["ruff check packages/lakestore"]
    assert "pytest" not in cmds
    assert "ruff check ." not in cmds


def test_commit_preflight_falls_back_to_global_when_no_persona_entry(tmp_path: Path) -> None:
    repo = RepoConfig(
        repo_root=tmp_path,
        default_persona="coder",
        verify_commands=["pytest"],
        persona_verify_commands={},
    )
    cmds = _commit_preflight_commands(repo, "coder")
    assert cmds == ["pytest"]


def test_commit_preflight_explicit_override_wins(tmp_path: Path) -> None:
    """commit_preflight_commands explicit override always takes priority."""
    repo = RepoConfig(
        repo_root=tmp_path,
        default_persona="coder",
        verify_commands=["pytest"],
        commit_preflight_commands=["make lint"],
        persona_verify_commands={"lakestore": ("ruff check packages/lakestore",)},
    )
    cmds = _commit_preflight_commands(repo, "lakestore")
    assert cmds == ["make lint"]


# ---------------------------------------------------------------------------
# Test B — allowed_paths threaded into fix-prompt build_agent_prompt call
# ---------------------------------------------------------------------------


def _make_stubbed_persona(name: str, allowed_paths: tuple[str, ...]) -> Persona:
    return Persona(
        name=name,
        prompt_path=Path("/dev/null"),
        allowed_tools=[],
        capabilities={},
        body="stub persona body",
        allowed_paths=allowed_paths,
    )


def test_review_fix_prompt_includes_allowed_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_agent_prompt must be called with allowed_paths for the fix persona."""
    _patch_level_up_root(monkeypatch, tmp_path / "level_up")
    allowed = ("packages/lakestore/",)
    repo = _repo_with_persona_verify(
        tmp_path,
        persona="coder",
        persona_allowed_paths=allowed,
    )
    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    backend = _CapturingBackend()
    stub_persona = _make_stubbed_persona("coder", allowed)

    captured_kwargs: list[dict] = []

    original_build = __import__(
        "agent_fleet.prompts.agent", fromlist=["build_agent_prompt"]
    ).build_agent_prompt

    def _capturing_build(**kwargs: object) -> object:
        captured_kwargs.append(kwargs)
        return original_build(**kwargs)

    with (
        patch("agent_fleet.pr_loop.lifecycle.make_backend", return_value=backend),
        patch("agent_fleet.pr_loop.lifecycle.has_blocking_findings", return_value=True),
        patch("agent_fleet.pr_loop.lifecycle.github_ops.pr_diff", return_value="+added"),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.pr_changed_files",
            return_value=["packages/lakestore/src/model.py"],
        ),
        patch("agent_fleet.pr_loop.lifecycle._git_changed_files", return_value=[]),
        patch("agent_fleet.pr_loop.lifecycle.build_agent_prompt", side_effect=_capturing_build),
        patch(
            "agent_fleet.pr_loop.lifecycle.YamlPersonaResolver.load",
            return_value=stub_persona,
        ),
    ):
        address_review_findings(
            pr_number=55,
            branch="fleet/coder/55-issue",
            review_body="blocking finding",
            repo=repo,
            loop_config=PrLoopConfig(enabled=True),
            fleet_config=fleet_config,
            worktree=tmp_path / "wt",
        )

    assert captured_kwargs, "build_agent_prompt was never called"
    kwargs = captured_kwargs[0]
    assert "allowed_paths" in kwargs, "allowed_paths not passed to build_agent_prompt"
    assert kwargs["allowed_paths"] == allowed, (
        f"expected {allowed!r}, got {kwargs['allowed_paths']!r}"
    )


def test_ci_fix_prompt_includes_allowed_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_agent_prompt must be called with allowed_paths in CI-fix path."""
    _patch_level_up_root(monkeypatch, tmp_path / "level_up")
    allowed = ("packages/lakestore/",)
    repo = _repo_with_persona_verify(
        tmp_path,
        persona="coder",
        persona_allowed_paths=allowed,
    )
    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    backend = _CapturingBackend()
    stub_persona = _make_stubbed_persona("coder", allowed)

    captured_kwargs: list[dict] = []

    original_build = __import__(
        "agent_fleet.prompts.agent", fromlist=["build_agent_prompt"]
    ).build_agent_prompt

    def _capturing_build(**kwargs: object) -> object:
        captured_kwargs.append(kwargs)
        return original_build(**kwargs)

    with (
        patch("agent_fleet.pr_loop.lifecycle.make_backend", return_value=backend),
        patch("agent_fleet.pr_loop.lifecycle._git_changed_files", return_value=[]),
        patch("agent_fleet.pr_loop.lifecycle.build_agent_prompt", side_effect=_capturing_build),
        patch(
            "agent_fleet.pr_loop.lifecycle.YamlPersonaResolver.load",
            return_value=stub_persona,
        ),
    ):
        attempt_ci_fix(
            pr_number=55,
            branch="fleet/coder/55-issue",
            failed_checks=["pytest"],
            repo=repo,
            loop_config=PrLoopConfig(enabled=True),
            fleet_config=fleet_config,
            worktree=tmp_path / "wt",
            persona="coder",
        )

    assert captured_kwargs, "build_agent_prompt was never called"
    kwargs = captured_kwargs[0]
    assert "allowed_paths" in kwargs, "allowed_paths not passed to build_agent_prompt"
    assert kwargs["allowed_paths"] == allowed, (
        f"expected {allowed!r}, got {kwargs['allowed_paths']!r}"
    )
