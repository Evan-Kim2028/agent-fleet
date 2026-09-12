"""Regression tests for LocalFleetRunner.run() resume (ResumableGitOps) path.

Before this fix, resuming an interrupted run (self._config.resume=True, the
default, plus a GitOps that implements find_resume_branch/attach_worktree —
see agent_fleet/integrations/local_git.py) was silently broken three ways:

1. ImplementHandler unconditionally called git_ops.setup_workspace(), which
   resolves to the *same* path attach_worktree just checked out and either
   destroyed it (rmtree-then-recreate) or crashed on `git worktree add -b
   <branch>` because the branch already exists.
2. SynthesizeHandler skipped synthesis on resume, leaving ctx.brief=None;
   ImplementHandler passed that straight to implement() -> _build_prompt(),
   which calls brief.to_dict() unconditionally (AttributeError).
3. The backend session was opened with cwd=repo_root (not the resumed
   worktree) and no way to carry a durable session id (e.g. a Devin CLI
   session, resumed via `-r`) across the interruption, so a resumed run's
   agent operated in the wrong directory and always started fresh.

These tests exercise LocalFleetRunner.run() end to end (phase functions
mocked, git_ops/backend are fakes) to prove all three are fixed.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import agent_fleet.session_store as session_store_module
from agent_fleet.config import FleetConfig
from agent_fleet.contracts.implementation_brief import ImplementationBrief
from agent_fleet.contracts.task_spec import DecompositionDecision, RiskTier, Scope, TaskSpec
from agent_fleet.contracts.verify_result import VerifyResult, VerifySeverity
from agent_fleet.hooks import Persona
from agent_fleet.level_up.models import DispatchEquip
from agent_fleet.noop_session import NoopLLMResult
from agent_fleet.runner import FleetRunConfig, LocalFleetRunner
from agent_fleet.session_store import persist_session_id

if TYPE_CHECKING:
    import pytest


class _FakeResumableGitOps:
    """GitOps + ResumableGitOps double.

    setup_workspace/create_branch raise if called: on a genuine resume they
    must not be — the whole point is reusing the worktree attach_worktree
    already checked out. Tests that exercise the non-resume path assert these
    stubs *were* called instead (see test_non_resume_run_still_calls_setup_workspace).
    """

    def __init__(self, *, resumed_worktree: Path, resumed_branch: str, resumed_run_id: str) -> None:
        self.use_worktree = True
        self._resumed_worktree = resumed_worktree
        self._resumed_branch = resumed_branch
        self._resumed_run_id = resumed_run_id
        self.setup_workspace_calls = 0
        self.create_branch_calls = 0
        self.find_resume_branch_calls = 0
        self.attach_worktree_calls = 0

    def find_resume_branch(
        self, task_id: int, persona: str, branch_prefix: str
    ) -> tuple[str, str] | None:
        del task_id, persona, branch_prefix
        self.find_resume_branch_calls += 1
        return (self._resumed_branch, self._resumed_run_id)

    def attach_worktree(self, branch_name: str, run_id: str, *, create: bool = True) -> Path | None:
        del branch_name, run_id, create
        self.attach_worktree_calls += 1
        return self._resumed_worktree

    def setup_workspace(self, *_a: object, **_k: object) -> Path:
        self.setup_workspace_calls += 1
        raise AssertionError("setup_workspace must not run during a resumed IMPLEMENT phase")

    def teardown_workspace(self, *_a: object, **_k: object) -> None:
        pass

    def create_branch(self, *_a: object, **_k: object) -> None:
        self.create_branch_calls += 1
        raise AssertionError("create_branch must not run during a resumed IMPLEMENT phase")

    def commit_changes(self, *_a: object, **_k: object) -> str | None:
        return None

    def changed_files(self, *_a: object, **_k: object) -> list[Path]:
        return []

    def diff_summary(self, *_a: object, **_k: object) -> str:
        return ""

    def push_branch(self, *_a: object, **_k: object) -> None:
        pass


class _NonResumingGitOps(_FakeResumableGitOps):
    """Same double, but find_resume_branch reports nothing to resume — the
    normal (non-resume) path, used as a control to prove setup_workspace is
    still called when there's genuinely nothing to resume."""

    def find_resume_branch(
        self, task_id: int, persona: str, branch_prefix: str
    ) -> tuple[str, str] | None:
        del task_id, persona, branch_prefix
        self.find_resume_branch_calls += 1
        return None

    def setup_workspace(self, *_a: object, **_k: object) -> Path:
        self.setup_workspace_calls += 1
        return self._resumed_worktree

    def create_branch(self, *_a: object, **_k: object) -> None:
        self.create_branch_calls += 1


class _FakePersonaResolver:
    def load(self, name: str, *, loadout_size: str | None = None) -> Persona:
        del loadout_size
        return Persona(
            name=name,
            prompt_path=Path("/dev/null"),
            allowed_tools=[],
            capabilities={},
            model=None,
            mcp_servers=[],
        )

    def list_personas(self) -> list[str]:
        return ["coder"]


class _FakeSessionBackend:
    """SessionCapableBackend double: records every create_session() call."""

    def __init__(self) -> None:
        self.create_session_calls: list[dict[str, Any]] = []

    def create_session(self, **kwargs: Any) -> MagicMock:  # noqa: ANN401
        self.create_session_calls.append(kwargs)
        session = MagicMock()
        session.agent_id = kwargs.get("session_id") or "fresh-agent-id"
        session.send.return_value = NoopLLMResult(
            stdout="ok", stderr="", exit_code=0, duration_s=0.1, agent_id=session.agent_id
        )
        return session

    def run(self, *_a: object, **_k: object) -> NoopLLMResult:
        raise AssertionError("session-capable backend should route through session.send()")


def _minimal_task_spec() -> TaskSpec:
    return TaskSpec(
        issue_number=1,
        decomposition_decision=DecompositionDecision.SINGLE,
        decomposition_reason="ok",
        child_issues_proposed=[],
        scope=Scope(allowed_paths=["src/"], forbidden_paths=[]),
        research_plan=[],
        acceptance_criteria=["pass"],
        risk_tier=RiskTier.LOW,
        critical_paths_touched=[],
        coordination_spec=None,
    )


def _minimal_brief() -> ImplementationBrief:
    return ImplementationBrief(
        issue_number=1,
        summary="resumed",
        files_to_create=[],
        files_to_modify=["src/foo.py"],
        test_strategy="none",
        acceptance_criteria=["pass"],
        references=[],
    )


def _ok_verify_result() -> VerifyResult:
    return VerifyResult(
        severity=VerifySeverity.OK,
        checks=[],
        violating_paths=[],
        files_changed=["src/foo.py"],
        message="ok",
    )


class _NoopExperienceRecorder:
    """No-op ExperienceRecorder — see agent_fleet/hooks.py's own docstring
    ("a test spy, or /dev/null"). Without this, LocalFleetRunner's default
    LevelUpRecorder writes real files under ~/.agent-fleet/level_up/ keyed by
    repo_root, which would be tmp_path in these tests — polluting the real
    user's fleet state with test artifacts on every run."""

    def record_runner_experience(self, **_kwargs: Any) -> None:  # noqa: ANN401
        pass

    def record_completed_task_experience(self, **_kwargs: Any) -> None:  # noqa: ANN401
        pass


def _common_patches(
    *, synthesize_spy: MagicMock, implement_spy: MagicMock, tmp_path: Path
) -> list[Any]:
    equip = DispatchEquip(
        skill_slots_execute=(),
        skill_slots_review=(),
        level_up_generation=0,
        compose_body="",
    )
    return [
        patch("agent_fleet.runner.plan", return_value=_minimal_task_spec()),
        patch("agent_fleet.runner.research_all", return_value=[]),
        patch("agent_fleet.runner.synthesize", synthesize_spy),
        patch("agent_fleet.runner.implement", implement_spy),
        patch("agent_fleet.runner.coerce_empty_decompose", side_effect=lambda ts: (ts, False)),
        patch("agent_fleet.runner.resolve_dispatch_equip", return_value=equip),
        patch("agent_fleet.runner.find_repo_config", return_value=None),
        patch("agent_fleet.runner.get_changed_files", return_value=["src/foo.py"]),
        patch("agent_fleet.runner.review", return_value=[]),
        # Keep RunLog's canonical run index/JSONL writes inside tmp_path — see
        # _NoopExperienceRecorder above for why level_up writes are separately
        # neutralized via the recorder rather than a path patch.
        patch("agent_fleet.observability.log._DEFAULT_RUNS_DIR", tmp_path / "runs"),
    ]


def test_resume_reuses_worktree_synthesizes_and_uses_resumed_session_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end regression guard for all three resume bugs at once:

    1. setup_workspace/create_branch are never called (worktree reused).
    2. synthesize() still runs so ctx.brief is populated before IMPLEMENT.
    3. The session is opened with cwd=<resumed worktree> and
       session_id=<the id persisted before the interruption>.
    """
    monkeypatch.setattr(session_store_module, "_STORE_DIR", tmp_path / "session_store")

    resumed_worktree = tmp_path / "worktrees" / "run-old"
    resumed_worktree.mkdir(parents=True)
    persist_session_id(str(resumed_worktree), "devin-old-session-id")

    git_ops = _FakeResumableGitOps(
        resumed_worktree=resumed_worktree,
        resumed_branch="fleet/coder/1-run-old",
        resumed_run_id="run-old",
    )
    backend = _FakeSessionBackend()
    synthesize_spy = MagicMock(return_value=_minimal_brief())
    implement_spy = MagicMock()
    verifier = MagicMock()
    verifier.check.return_value = _ok_verify_result()

    fleet_config = FleetConfig(default_backend="devin", default_model=None, mcp_servers={})

    runner = LocalFleetRunner(
        backend=backend,
        persona_resolver=_FakePersonaResolver(),
        git_ops=git_ops,
        verifier=verifier,
        fleet_config=fleet_config,
        config=FleetRunConfig(resume=True),
        experience_recorder=_NoopExperienceRecorder(),
    )

    patches = _common_patches(
        synthesize_spy=synthesize_spy, implement_spy=implement_spy, tmp_path=tmp_path
    )
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        runner.run(
            task_id=1,
            title="resumed task",
            body="continue the fix",
            persona="coder",
            repo_root=tmp_path,
            base_branch="main",
        )

    # Bug 1: worktree/branch reused, never recreated.
    assert git_ops.setup_workspace_calls == 0
    assert git_ops.create_branch_calls == 0
    assert git_ops.find_resume_branch_calls == 1
    assert git_ops.attach_worktree_calls == 1

    # Bug 2: SYNTHESIZE still ran, so IMPLEMENT got a real brief, not None.
    assert synthesize_spy.called
    assert implement_spy.called
    brief_arg = implement_spy.call_args[0][0]
    assert brief_arg is not None
    assert brief_arg.summary == "resumed"

    # Bug 3: session opened against the resumed worktree, resuming the
    # persisted session id instead of starting fresh with cwd=repo_root.
    assert len(backend.create_session_calls) == 1
    call = backend.create_session_calls[0]
    assert call["cwd"] == resumed_worktree
    assert call["session_id"] == "devin-old-session-id"


def test_resume_without_prior_session_id_still_uses_resumed_worktree_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resume with no session_store entry (e.g. a non-devin backend, or the
    interrupted run never got far enough to capture one): cwd must still be
    the resumed worktree, and session_id must be None (not stale/garbage)."""
    monkeypatch.setattr(session_store_module, "_STORE_DIR", tmp_path / "session_store")

    resumed_worktree = tmp_path / "worktrees" / "run-old2"
    resumed_worktree.mkdir(parents=True)

    git_ops = _FakeResumableGitOps(
        resumed_worktree=resumed_worktree,
        resumed_branch="fleet/coder/1-run-old2",
        resumed_run_id="run-old2",
    )
    backend = _FakeSessionBackend()
    verifier = MagicMock()
    verifier.check.return_value = _ok_verify_result()
    fleet_config = FleetConfig(default_backend="devin", default_model=None, mcp_servers={})

    runner = LocalFleetRunner(
        backend=backend,
        persona_resolver=_FakePersonaResolver(),
        git_ops=git_ops,
        verifier=verifier,
        fleet_config=fleet_config,
        config=FleetRunConfig(resume=True),
        experience_recorder=_NoopExperienceRecorder(),
    )

    patches = _common_patches(
        synthesize_spy=MagicMock(return_value=_minimal_brief()),
        implement_spy=MagicMock(),
        tmp_path=tmp_path,
    )
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        runner.run(
            task_id=1,
            title="resumed task",
            body="continue",
            persona="coder",
            repo_root=tmp_path,
            base_branch="main",
        )

    assert git_ops.setup_workspace_calls == 0
    call = backend.create_session_calls[0]
    assert call["cwd"] == resumed_worktree
    assert call["session_id"] is None


def test_non_resume_run_still_calls_setup_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: when there's genuinely nothing to resume, the normal
    setup_workspace/create_branch path must still run, and the session cwd
    must be repo_root — proving the resume branch didn't leak into the
    default path."""
    monkeypatch.setattr(session_store_module, "_STORE_DIR", tmp_path / "session_store")

    fresh_worktree = tmp_path / "worktrees" / "run-new"
    fresh_worktree.mkdir(parents=True)

    git_ops = _NonResumingGitOps(
        resumed_worktree=fresh_worktree, resumed_branch="unused", resumed_run_id="unused"
    )
    backend = _FakeSessionBackend()
    verifier = MagicMock()
    verifier.check.return_value = _ok_verify_result()
    fleet_config = FleetConfig(default_backend="devin", default_model=None, mcp_servers={})

    runner = LocalFleetRunner(
        backend=backend,
        persona_resolver=_FakePersonaResolver(),
        git_ops=git_ops,
        verifier=verifier,
        fleet_config=fleet_config,
        config=FleetRunConfig(resume=True),
        experience_recorder=_NoopExperienceRecorder(),
    )

    patches = _common_patches(
        synthesize_spy=MagicMock(return_value=_minimal_brief()),
        implement_spy=MagicMock(),
        tmp_path=tmp_path,
    )
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        runner.run(
            task_id=2,
            title="fresh task",
            body="do it",
            persona="coder",
            repo_root=tmp_path,
            base_branch="main",
        )

    assert git_ops.setup_workspace_calls == 1
    assert git_ops.create_branch_calls == 0  # use_worktree=True path: branch created via -b flag
    call = backend.create_session_calls[0]
    assert call["cwd"] == tmp_path
    assert call["session_id"] is None
