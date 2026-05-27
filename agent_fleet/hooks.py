"""Protocol seams for fleet backends, personas, git, and verification."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from agent_fleet.agent_mode import AgentMode
    from agent_fleet.contracts.mcp import McpServerSpec
    from agent_fleet.contracts.mcp_requirement import McpRequirement
    from agent_fleet.contracts.verify_result import VerifyResult
    from agent_fleet.level_up.models import DispatchEquip


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
    @property
    def usage(self) -> Mapping[str, int] | None: ...


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
class LLMSession(Protocol):
    """A durable agent handle scoped to a single task.

    Multiple phases call send() on the same session so that MCP connections
    and agent_id persist across plan → research → synthesize → implement →
    verify → review.
    """

    agent_id: str | None

    def send(
        self,
        prompt: str,
        *,
        max_tokens: int,
        timeout_s: int,
        allowed_tools: list[str] | None = None,
        expect_mcp_tools: bool = False,
        mcp_requirement: McpRequirement | None = None,
    ) -> LLMResult: ...

    def dispose(self) -> None: ...


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

    def setup_workspace(
        self,
        repo_root: Path,
        run_id: str,
        base_branch: str,
        *,
        branch_name: str | None = None,
    ) -> Path: ...
    def teardown_workspace(self, worktree: Path, *, forensic: bool = False) -> None: ...
    def create_branch(self, worktree: Path, branch_name: str) -> None: ...
    def commit_changes(self, worktree: Path, message: str) -> str | None: ...
    def push_branch(self, worktree: Path, branch_name: str) -> None: ...
    def changed_files(self, worktree: Path) -> list[Path]: ...
    def diff_summary(self, worktree: Path) -> str: ...


@runtime_checkable
class SessionCapableBackend(LLMBackend, Protocol):
    """Backends that expose durable MCP-aware sessions (e.g. Cursor SDK)."""

    def create_session(
        self,
        *,
        persona_name: str,
        cwd: Path,
        mcp_servers: Mapping[str, McpServerSpec] | None = None,
        model: str | None = None,
        mode: AgentMode | str | None = None,
    ) -> LLMSession: ...


@runtime_checkable
class ResumableGitOps(GitOps, Protocol):
    """GitOps extensions used when resuming interrupted fleet runs."""

    def find_resume_branch(
        self,
        task_id: int,
        persona: str,
        branch_prefix: str,
    ) -> tuple[str, str] | None: ...

    def attach_worktree(
        self,
        branch_name: str,
        run_id: str,
        *,
        create: bool = True,
    ) -> Path | None: ...


@dataclass(frozen=True)
class Persona:
    name: str
    prompt_path: Path
    allowed_tools: list[str]
    capabilities: dict[str, bool]
    body: str | None = None
    skill_slots_execute: tuple[str, ...] = ()
    skill_slots_review: tuple[str, ...] = ()
    level_up_generation: int = 0
    allowed_paths: tuple[str, ...] = ()
    model: str = "composer-2.5"
    mode: str = "agent"
    extra_instructions: str = ""
    mcp_servers: list[str] = field(default_factory=list)


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
    equip: DispatchEquip | None = None


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
    phases: dict[str, object] | None = None
    task_spec: dict[str, object] | None = None
    changed_files: list[str] | None = None
    worktree: str | None = None
    branch_name: str | None = None
    pr_number: int | None = None
    pr_loop_status: str | None = None
    stderr: str = ""
    files_modified: tuple[str, ...] = ()
