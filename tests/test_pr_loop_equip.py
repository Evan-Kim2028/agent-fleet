"""PR loop fix agents include dispatch equip compose body."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_fleet.config import load_fleet_config
from agent_fleet.noop_session import NoopLLMResult
from agent_fleet.pr_loop.config import PrLoopConfig
from agent_fleet.pr_loop.lifecycle import address_review_findings, attempt_ci_fix
from agent_fleet.repo import RepoConfig, load_repo_config

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
            stdout="done",
            stderr="",
            exit_code=0,
            duration_s=0.1,
            agent_id="fix-agent",
        )


def _patch_level_up_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr("agent_fleet.level_up.paths.LEVEL_UP_ROOT", root)


def _pr_loop_repo(tmp_path: Path) -> RepoConfig:
    repo_yaml = tmp_path / ".agent-fleet.yaml"
    repo_yaml.write_text(
        "name: pr-loop-equip\npr_loop:\n  enabled: true\n",
        encoding="utf-8",
    )
    return load_repo_config(repo_yaml)


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    wt.mkdir()
    return wt


def test_review_fix_prompt_includes_fix_ci_skill(
    tmp_path: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_level_up_root(monkeypatch, tmp_path / "level_up")
    repo = _pr_loop_repo(tmp_path)
    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    backend = _CapturingBackend()
    review_body = "**Risk Level:** MEDIUM\n<details><summary>MEDIUM</summary>"

    with (
        patch("agent_fleet.pr_loop.lifecycle.make_backend", return_value=backend),
        patch(
            "agent_fleet.pr_loop.lifecycle.has_blocking_findings",
            return_value=True,
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.pr_diff",
            return_value="+added",
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.pr_changed_files",
            return_value=["src/a.py"],
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle._git_changed_files",
            return_value=[],
        ),
    ):
        result = address_review_findings(
            pr_number=42,
            branch="fleet/coder/42-abc",
            review_body=review_body,
            repo=repo,
            loop_config=PrLoopConfig(enabled=True),
            fleet_config=fleet_config,
            worktree=worktree,
        )

    assert result.status == "no_changes"
    assert len(backend.prompts) == 1
    prompt = backend.prompts[0]
    assert "# Persona" in prompt
    assert "# Fix CI" in prompt
    assert "# Review" in prompt
    assert review_body in prompt
    assert "- (none)" in prompt


def test_ci_fix_prompt_includes_fix_ci_skill(
    tmp_path: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_level_up_root(monkeypatch, tmp_path / "level_up")
    repo = _pr_loop_repo(tmp_path)
    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    backend = _CapturingBackend()

    with (
        patch("agent_fleet.pr_loop.lifecycle.make_backend", return_value=backend),
        patch(
            "agent_fleet.pr_loop.lifecycle._git_changed_files",
            return_value=[],
        ),
    ):
        result = attempt_ci_fix(
            pr_number=7,
            branch="fleet/coder/7-def",
            failed_checks=["pytest"],
            repo=repo,
            loop_config=PrLoopConfig(enabled=True),
            fleet_config=fleet_config,
            worktree=worktree,
            persona="coder",
        )

    assert not result.ok
    assert result.phase == "no_changes"
    assert len(backend.prompts) == 1
    prompt = backend.prompts[0]
    assert "# Fix CI" in prompt
    assert "Failed checks: pytest" in prompt
    assert "- (none)" in prompt


def test_ci_fix_journals_equip_with_run_id(
    tmp_path: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_fleet.level_up.paths import persona_dir

    _patch_level_up_root(monkeypatch, tmp_path / "level_up")
    repo = _pr_loop_repo(tmp_path)
    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    backend = _CapturingBackend()

    with (
        patch("agent_fleet.pr_loop.lifecycle.make_backend", return_value=backend),
        patch(
            "agent_fleet.pr_loop.lifecycle._git_changed_files",
            return_value=[],
        ),
    ):
        attempt_ci_fix(
            pr_number=7,
            branch="fleet/coder/7-def",
            failed_checks=["pytest"],
            repo=repo,
            loop_config=PrLoopConfig(enabled=True),
            fleet_config=fleet_config,
            worktree=worktree,
            persona="coder",
        )

    journal_path = persona_dir("pr-loop-equip", "coder") / "journal.jsonl"
    assert journal_path.is_file()
    rows = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    assert any(row.get("run_id") == "pr-loop-7" for row in rows)


def test_review_fix_journals_equip_with_run_id(
    tmp_path: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_fleet.level_up.paths import persona_dir

    _patch_level_up_root(monkeypatch, tmp_path / "level_up")
    repo = _pr_loop_repo(tmp_path)
    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    backend = _CapturingBackend()

    with (
        patch("agent_fleet.pr_loop.lifecycle.make_backend", return_value=backend),
        patch(
            "agent_fleet.pr_loop.lifecycle.has_blocking_findings",
            return_value=True,
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.pr_diff",
            return_value="+added",
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.pr_changed_files",
            return_value=["src/a.py"],
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle._git_changed_files",
            return_value=[],
        ),
    ):
        address_review_findings(
            pr_number=99,
            branch="fleet/coder/99-xyz",
            review_body="blocking finding",
            repo=repo,
            loop_config=PrLoopConfig(enabled=True),
            fleet_config=fleet_config,
            worktree=worktree,
        )

    journal_path = persona_dir("pr-loop-equip", "coder") / "journal.jsonl"
    assert journal_path.is_file()
    rows = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    assert any(row.get("run_id") == "pr-loop-99" for row in rows)
