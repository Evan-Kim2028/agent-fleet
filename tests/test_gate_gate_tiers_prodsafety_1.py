"""Tier 0 approves a PR that deletes the tests it touched, having run none of them.

Tier 0 exists on one piece of evidence: the PR's *own* changed tests, green at
head (pipeline.py, ``tier0_eligible``). A test-only PR therefore gets no model
review, and the approval rests entirely on step0 having run something.

``changed_test_files`` keeps only changed ``test_*.py`` paths that still exist,
so a PR whose change is a *deletion* has an empty step0 set: ``run_pr_tests``
returns ``[]`` without running anything, ``_step0_run`` stays ``None``, and
there is no failure to record. Every remaining precondition passes — the diff is
docs/tests only and ``evidence.confirmed`` is empty — so ``run()`` returns
APPROVED having executed zero tests and dispatched zero reviewers.

The same holds for a PR whose only change is suite-level test config
(``tests/conftest.py``), which is not a ``test_*.py`` at all.

Both assertions below are the guard the claim asks for: a PR that changed a test
file and ran none of them must not be tier 0. The first is written so it fails
on the verdict and not on the mechanism, so the fixed code passes it whatever
form the guard takes.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agent_fleet.gate.config import GateConfig
from agent_fleet.gate.gitops import changed_test_files
from agent_fleet.gate.pipeline import (
    APPROVAL_MARKER,
    GateOutcome,
    GatePipeline,
    PullRequestRef,
)
from agent_fleet.model_policy import ModelPolicy

# ---------------------------------------------------------------------------
# Fakes — no network, no real model, no real pytest
# ---------------------------------------------------------------------------


@dataclass
class _FakeBackend:
    """Replays one answer per call and records every prompt it was asked."""

    answer: str = json.dumps({"findings": []})
    prompts: list[str] = field(default_factory=list)

    def run(self, prompt: str, **_kwargs: Any) -> Any:  # noqa: ANN401
        self.prompts.append(prompt)
        return _FakeResult(self.answer)


@dataclass(frozen=True)
class _FakeResult:
    stdout: str
    stderr: str = ""
    exit_code: int = 0
    duration_s: float = 0.0
    agent_id: str | None = None
    usage: dict[str, int] | None = None


@dataclass(frozen=True)
class _FakePytest:
    """A pytest run that worked and found nothing — the step0 "green" answer.

    Standing in for the runner keeps the PR's own pytest plugins out of the
    picture: what is under test here is the tier-0 decision, not the runner.
    """

    ran: int = 0
    ran_files: list[tuple[str, ...]] = field(default_factory=list)

    def run(self, test_files: list[str]) -> Any:  # noqa: ANN401
        from agent_fleet.gate.pipeline import TestRun

        self.ran_files.append(tuple(test_files))
        return TestRun(ran=self.ran)

    def packages_for(self, test_files: list[str]) -> list[Any]:  # noqa: ANN401
        return []

    def __call__(self, _worktree: Path) -> _FakePytest:
        return self


def _config(**overrides: Any) -> GateConfig:  # noqa: ANN401
    base: dict[str, Any] = {
        "backend": "cmd",
        "model": "m",
        "judge_backend": "cmd",
        "judge_model": "m",
        "enable_judge": False,
        "enable_fix": False,
        "lens_timeout_s": 10,
        "verify_timeout_s": 10,
        "judge_timeout_s": 10,
        "fix_timeout_s": 10,
        "test_timeout_s": 10,
    }
    base.update(overrides)
    return GateConfig(**base)


def _pipeline(tmp_path: Path, backend: _FakeBackend, repo: Path) -> GatePipeline:
    return GatePipeline(
        repo=repo,
        pr_number=42,
        config=_config(),
        policy=ModelPolicy(backends={}),
        backend=backend,  # type: ignore[arg-type]
        gate_dir=tmp_path / "gate",
        use_systemd=False,
    )


# ---------------------------------------------------------------------------
# A real repo whose only PR change is a test-file deletion
# ---------------------------------------------------------------------------


def _git_env(tmp_path: Path) -> dict[str, str]:
    return {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(tmp_path),
    }


def _git(repo: Path, env: dict[str, str], *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return completed.stdout


@pytest.fixture
def repo_with_deleted_test(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A repo on ``main``+1 whose sole change is ``git rm tests/test_core.py``.

    The regression test guarding a data-loss bug is gone and nothing replaced
    it — the shape of diff ``prodsafety_1`` describes.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    env = _git_env(tmp_path)
    _git(repo, env, "init", "-q", "-b", "main")
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (repo / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "a.md").write_text("a\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_core.py").write_text(
        "def test_regression():\n    assert True\n", encoding="utf-8"
    )
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "-qm", "base")
    _git(repo, env, "checkout", "-qb", "fb/lane")
    _git(repo, env, "rm", "-q", "tests/test_core.py")
    _git(repo, env, "commit", "-qm", "delete the regression test")
    return repo, env


# ---------------------------------------------------------------------------
# The claim
# ---------------------------------------------------------------------------


def test_a_pr_that_deletes_its_own_test_is_not_approved_on_no_tests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_with_deleted_test: tuple[Path, dict[str, str]],
) -> None:
    repo, env = repo_with_deleted_test

    # The evidence really is a deletion: the diff names the test, the file is gone,
    # and the selector therefore reports no test to run.
    assert _git(repo, env, "diff", "--name-only", "main...HEAD").split() == [
        "tests/test_core.py"
    ]
    assert not (repo / "tests" / "test_core.py").exists()
    assert changed_test_files(repo, "main") == []

    head_sha = _git(repo, env, "rev-parse", "HEAD").strip()
    monkeypatch.setattr(
        "agent_fleet.gate.pipeline.resolve_pull_request",
        lambda *_a, **_k: PullRequestRef(
            number=42,
            head_ref="fb/lane",
            head_sha=head_sha,
            state="OPEN",
            base_ref="main",
        ),
    )
    # HOME is redirected so the gate's metrics append lands in tmp_path, not
    # the operator's real ~/.agent-fleet.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AGENT_FLEET_HOME", raising=False)

    backend = _FakeBackend()
    pipe = _pipeline(tmp_path, backend, repo)
    runner = _FakePytest()
    monkeypatch.setattr(pipe, "_runner_for", runner)

    # step0 finds no test to run, so there is no run and no failure to record.
    assert pipe.run_pr_tests(repo) == []
    assert pipe._step0_run is None
    assert pipe.evidence.confirmed == []

    result = pipe.run()

    # Tier 0 approves on evidence: the PR's own tests, green at head. Here zero
    # of the PR's tests were run — the one it deleted no longer exists — so an
    # approval is a statement about nothing. It must not be issued.
    assert not result.approved, (
        f"PR deletes its only test yet was approved with tier 0: "
        f"{result.outcome!r} / {result.reasons} / status={result.status_line!r}"
    )
    assert result.outcome is not GateOutcome.APPROVED
    assert APPROVAL_MARKER not in result.status_line
