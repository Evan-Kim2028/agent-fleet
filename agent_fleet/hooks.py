"""Protocol seams for fleet backends, personas, git, and verification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from agent_fleet.agent_mode import AgentMode
    from agent_fleet.contracts.verify_result import VerifyResult


@runtime_checkable
class LLMResult(Protocol):
    @property
    def stdout(self) -> str: ...
    @property
    def stderr(self) -> str: ...
    @property
    def exit_code(self) -> int: ...
    @property
    def duration_s(self) -> float: ...
    @property
    def agent_id(self) -> str | None: ...


@runtime_checkable
class LLMBackend(Protocol):
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
    ) -> LLMResult: ...


@runtime_checkable
class Verifier(Protocol):
    def check(
        self,
        worktree: Path,
        *,
        persona: str,
        changed_files: list[Path],
        task_id: int,
    ) -> VerifyResult: ...


@runtime_checkable
class GitForge(Protocol):
    """Optional forge integration (GitHub, GitLab). Omit for local-only runs."""

    def open_pr(
        self,
        *,
        title: str,
        body: str,
        branch: str,
        base: str,
        draft: bool,
        labels: list[str],
    ) -> int: ...

    def mark_ready(self, pr_number: int) -> None: ...
    def comment(self, issue_or_pr: int, body: str) -> None: ...
    def get_labels(self, issue_or_pr: int) -> list[str]: ...


@runtime_checkable
class GitOps(Protocol):
    """Repo-local git operations (worktree, diff, commit)."""

    def setup_workspace(self, repo_root: Path, run_id: str, base_branch: str) -> Path: ...
    def teardown_workspace(self, worktree: Path, *, forensic: bool = False) -> None: ...
    def create_branch(self, worktree: Path, branch_name: str) -> None: ...
    def commit_changes(self, worktree: Path, message: str) -> str | None: ...
    def push_branch(self, worktree: Path, branch_name: str) -> None: ...
    def changed_files(self, worktree: Path) -> list[Path]: ...
    def diff_summary(self, worktree: Path) -> str: ...


@dataclass(frozen=True)
class Persona:
    name: str
    prompt_path: Path
    allowed_tools: list[str]
    capabilities: dict[str, bool]
    allowed_paths: tuple[str, ...] = ()
    model: str = "composer-2.5"
    mode: str = "agent"
    extra_instructions: str = ""


@runtime_checkable
class PersonaResolver(Protocol):
    def load(self, name: str) -> Persona: ...
    def list_personas(self) -> list[str]: ...


@dataclass(frozen=True)
class ExecutorResult:
    stdout: str
    stderr: str = ""
    exit_code: int = 0
    duration_s: float = 0.0
    agent_id: str | None = None


@runtime_checkable
class AgentExecutor(Protocol):
    def execute(
        self,
        phase_name: str,
        prompt: str,
        *,
        context: dict[str, Any] | None = None,
        attachments: Sequence[Path] = (),
    ) -> ExecutorResult: ...


class LLMBackendExecutor:
    def __init__(self, backend: LLMBackend) -> None:
        self._backend = backend

    def execute(
        self,
        phase_name: str,
        prompt: str,
        *,
        context: dict[str, Any] | None = None,
        attachments: Sequence[Path] = (),
    ) -> ExecutorResult:
        del phase_name
        if attachments:
            raise NotImplementedError("LLMBackendExecutor does not support image attachments")
        ctx = context or {}
        result = self._backend.run(
            prompt,
            max_tokens=int(ctx.get("max_tokens", 4096)),
            timeout_s=int(ctx.get("timeout_s", 1800)),
            memory_limit=str(ctx.get("memory_limit", "4G")),
            allowed_tools=list(ctx.get("allowed_tools", [])),
            cwd=ctx.get("cwd"),
            model=ctx.get("model"),
            mode=ctx.get("mode"),
        )
        return ExecutorResult(
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            duration_s=result.duration_s,
            agent_id=getattr(result, "agent_id", None),
        )


@dataclass(frozen=True)
class FleetTask:
    goal: str
    context: str = ""
    persona: str = "coder"
    workspace: str | None = None
    pipeline: str | None = None
    title: str | None = None


@dataclass(frozen=True)
class FleetTaskResult:
    task_index: int
    persona: str
    goal: str
    status: str
    summary: str | None
    error: str | None
    duration_seconds: float
    agent_id: str | None = None
    phases: dict[str, Any] | None = None
    task_spec: dict[str, Any] | None = None
    changed_files: list[str] | None = None
