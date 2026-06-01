"""Mock test doubles for agent_fleet hook Protocols."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_fleet.contracts.verify_result import VerifyResult, VerifySeverity
from agent_fleet.hooks import Persona


@dataclass
class _FakeLLMResult:
    stdout: str
    stderr: str = ""
    exit_code: int = 0
    duration_s: float = 0.1


@dataclass
class MockLLMBackend:
    responses: list[str] = field(default_factory=lambda: [""])
    exit_codes: list[int] = field(default_factory=lambda: [0])
    durations: list[float] = field(default_factory=lambda: [0.1])
    calls: list[dict[str, Any]] = field(default_factory=list)

    def run(
        self,
        prompt: str,
        *,
        max_tokens: int,
        timeout_s: int,
        memory_limit: str,
        allowed_tools: list[str],
        cwd: Path | None = None,
    ) -> _FakeLLMResult:
        idx = min(len(self.calls), len(self.responses) - 1)
        exit_idx = min(len(self.calls), len(self.exit_codes) - 1)
        duration_idx = min(len(self.calls), len(self.durations) - 1)
        self.calls.append(
            {
                "prompt": prompt,
                "max_tokens": max_tokens,
                "timeout_s": timeout_s,
                "memory_limit": memory_limit,
                "allowed_tools": allowed_tools,
                "cwd": cwd,
            }
        )
        return _FakeLLMResult(
            stdout=self.responses[idx],
            exit_code=self.exit_codes[exit_idx],
            duration_s=self.durations[duration_idx],
        )


@dataclass
class MockVerifier:
    result: VerifyResult = field(
        default_factory=lambda: VerifyResult(
            severity=VerifySeverity.OK,
            checks=[],
            violating_paths=[],
            files_changed=[],
            message="mock ok",
        )
    )

    def check(
        self,
        worktree: Path,
        *,
        persona: str,
        changed_files: list[Path],
        issue_number: int,
    ) -> VerifyResult:
        return self.result


@dataclass
class MockGitForge:
    pr_number: int = 1
    opened_prs: list[dict[str, Any]] = field(default_factory=list)
    comments: list[tuple[int, str]] = field(default_factory=list)
    labels: dict[int, list[str]] = field(default_factory=dict)
    marked_ready: list[int] = field(default_factory=list)

    def open_pr(
        self,
        *,
        title: str,
        body: str,
        branch: str,
        base: str,
        draft: bool,
        labels: list[str],
    ) -> int:
        self.opened_prs.append(
            {
                "title": title,
                "body": body,
                "branch": branch,
                "base": base,
                "draft": draft,
                "labels": labels,
            }
        )
        return self.pr_number

    def mark_ready(self, pr_number: int) -> None:
        self.marked_ready.append(pr_number)

    def comment(self, issue_or_pr: int, body: str) -> None:
        self.comments.append((issue_or_pr, body))

    def get_labels(self, issue_or_pr: int) -> list[str]:
        return self.labels.get(issue_or_pr, [])


@dataclass
class MockPersonaResolver:
    personas: dict[str, Persona] = field(default_factory=dict)

    def load(self, name: str) -> Persona:
        if name not in self.personas:
            self.personas[name] = Persona(
                name=name,
                prompt_path=Path(f"/tmp/fake_{name}.md"),
                allowed_tools=["read_file", "write_file"],
                capabilities={"can_write_tests": True},
                allowed_paths=(),
            )
        return self.personas[name]
