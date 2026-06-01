"""Runner-level tests for disposition wiring.

Focuses on _apply_disposition — the IO seam between decide_disposition and
the forge/git_ops integrations. Uses fake doubles matching the GitOps and
GitForge protocols so no real git or HTTP calls occur.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from agent_fleet.disposition import (
    DispositionKind,
    DispositionPolicy,
    RunFacts,
    decide_disposition,
)
from agent_fleet.observability.log import RunLog
from agent_fleet.runner import LocalFleetRunner

if TYPE_CHECKING:
    from agent_fleet.hooks import GitForge


class _FakeGitOps:
    """Minimal GitOps double — only push_branch and the required stubs."""

    def __init__(self) -> None:
        self.pushed: list[str] = []

    def push_branch(self, worktree: Path, branch_name: str) -> None:
        del worktree
        self.pushed.append(branch_name)

    def setup_workspace(self, *_a: object, **_k: object) -> Path:
        return Path("/tmp/wt")

    def teardown_workspace(self, *_a: object, **_k: object) -> None:
        pass

    def create_branch(self, *_a: object, **_k: object) -> None:
        pass

    def commit_changes(self, *_a: object, **_k: object) -> str | None:
        return None

    def changed_files(self, *_a: object, **_k: object) -> list[Path]:
        return []

    def diff_summary(self, *_a: object, **_k: object) -> str:
        return ""


class _FakeForge:
    def __init__(self, pr_number: int = 42) -> None:
        self._pr_number = pr_number
        self.open_pr_calls: list[dict] = []
        self.comments: list[tuple[int, str]] = []

    def open_pr(self, **kwargs: object) -> int:
        self.open_pr_calls.append(dict(kwargs))
        return self._pr_number

    def mark_ready(self, pr_number: int) -> None:
        del pr_number

    def comment(self, issue_or_pr: int, body: str) -> None:
        self.comments.append((issue_or_pr, body))

    def get_labels(self, issue_or_pr: int) -> list[str]:
        del issue_or_pr
        return []


def _make_runner(forge: GitForge | None = None) -> LocalFleetRunner:
    backend = MagicMock()
    persona_resolver = MagicMock()
    return LocalFleetRunner(
        backend=backend,
        persona_resolver=persona_resolver,
        git_ops=_FakeGitOps(),
        verifier=MagicMock(),
        forge=forge,
    )


def _make_run_log(tmp_path: Path) -> RunLog:
    return RunLog.create(
        run_id="test-run",
        task_id=1,
        persona="coder",
        runs_dir=tmp_path,
        include_memory_ring=False,
    )


def test_apply_disposition_salvage_opens_draft_pr(tmp_path: Path) -> None:
    forge = _FakeForge(pr_number=99)
    runner = _make_runner(forge=forge)
    run_log = _make_run_log(tmp_path)

    facts = RunFacts(
        verify_ok=False,
        verify_fatal=False,
        scope_violated=False,
        changed_files=("src/foo.py",),
    )
    policy = DispositionPolicy()
    disp = decide_disposition(facts, policy)

    assert disp.kind == DispositionKind.SALVAGE
    assert disp.draft is True

    pr = runner._apply_disposition(
        disp,
        worktree=tmp_path,
        branch_name="fleet/coder/1-abc",
        base_branch="main",
        pr_title=None,
        pr_body="body",
        pr_labels=["fleet-ready"],
        policy=policy,
        run_log=run_log,
        run_id="test-run",
    )

    assert pr == 99
    assert len(forge.open_pr_calls) == 1
    call = forge.open_pr_calls[0]
    assert call["draft"] is True
    assert "fleet-salvage" in call["labels"]


def test_apply_disposition_open_pr_not_draft(tmp_path: Path) -> None:
    forge = _FakeForge(pr_number=7)
    runner = _make_runner(forge=forge)
    run_log = _make_run_log(tmp_path)

    facts = RunFacts(
        verify_ok=True,
        verify_fatal=False,
        scope_violated=False,
        changed_files=("src/foo.py",),
    )
    policy = DispositionPolicy()
    disp = decide_disposition(facts, policy)

    assert disp.kind == DispositionKind.OPEN_PR
    assert disp.draft is False

    pr = runner._apply_disposition(
        disp,
        worktree=tmp_path,
        branch_name="fleet/coder/1-abc",
        base_branch="main",
        pr_title="Fix it",
        pr_body="body",
        pr_labels=["fleet-ready"],
        policy=policy,
        run_log=run_log,
        run_id="test-run",
    )

    assert pr == 7
    call = forge.open_pr_calls[0]
    assert call["draft"] is False
    assert "fleet-salvage" not in call["labels"]


def test_apply_disposition_abandon_returns_none(tmp_path: Path) -> None:
    forge = _FakeForge()
    runner = _make_runner(forge=forge)
    run_log = _make_run_log(tmp_path)

    facts = RunFacts(
        verify_ok=False,
        verify_fatal=True,
        scope_violated=False,
        changed_files=("src/foo.py",),
    )
    policy = DispositionPolicy()
    disp = decide_disposition(facts, policy)

    assert disp.kind == DispositionKind.ABANDON

    pr = runner._apply_disposition(
        disp,
        worktree=tmp_path,
        branch_name="fleet/coder/1-abc",
        base_branch="main",
        pr_title=None,
        pr_body="body",
        pr_labels=[],
        policy=policy,
        run_log=run_log,
        run_id="test-run",
    )

    assert pr is None
    assert forge.open_pr_calls == []


def test_apply_disposition_no_forge_returns_none(tmp_path: Path) -> None:
    runner = _make_runner(forge=None)
    run_log = _make_run_log(tmp_path)

    facts = RunFacts(
        verify_ok=False,
        verify_fatal=False,
        scope_violated=False,
        changed_files=("src/foo.py",),
    )
    disp = decide_disposition(facts, DispositionPolicy())

    assert disp.kind == DispositionKind.SALVAGE

    pr = runner._apply_disposition(
        disp,
        worktree=tmp_path,
        branch_name="fleet/coder/1-abc",
        base_branch="main",
        pr_title=None,
        pr_body="body",
        pr_labels=[],
        policy=DispositionPolicy(),
        run_log=run_log,
        run_id="test-run",
    )

    assert pr is None


@pytest.mark.parametrize("changed", [("src/a.py",), ()])
def test_verify_failed_disposition_kind(changed: tuple[str, ...]) -> None:
    facts = RunFacts(
        verify_ok=False,
        verify_fatal=False,
        scope_violated=False,
        changed_files=changed,
    )
    policy = DispositionPolicy()
    disp = decide_disposition(facts, policy)
    if changed:
        assert disp.kind == DispositionKind.SALVAGE
        assert disp.draft is True
        assert disp.outcome == "verify_failed_salvaged"
    else:
        assert disp.kind == DispositionKind.NOOP
        assert disp.outcome == "completed_noop"


def test_noop_facts_yield_completed_noop_not_completed() -> None:
    """Runner NOOP branch must use verify_ok=False so outcome is 'completed_noop'.

    Regression guard: passing verify_ok=True to decide_disposition returns
    outcome='completed', silently losing the 'completed_noop' signal that
    emit.py and dispatch.py treat as a distinct case.
    """
    noop_facts = RunFacts(
        verify_ok=False,
        verify_fatal=False,
        scope_violated=False,
        changed_files=(),
    )
    disp = decide_disposition(noop_facts, DispositionPolicy())
    assert disp.kind == DispositionKind.NOOP
    assert disp.outcome == "completed_noop", (
        "outcome must be 'completed_noop'; got 'completed' if verify_ok=True was passed"
    )
