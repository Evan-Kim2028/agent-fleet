"""Phase runners for coding fleet pipelines."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_fleet.agent_mode import parse_agent_mode

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.hooks import FleetTask, LLMBackend, Persona
    from agent_fleet.personas import YamlPersonaResolver


def _read_persona_prompt(persona: Persona) -> str:
    return persona.prompt_path.read_text(encoding="utf-8")


def _build_execute_prompt(persona: Persona, task: FleetTask) -> str:
    body = _read_persona_prompt(persona)
    parts = [
        "# Persona",
        body.strip(),
    ]
    if persona.extra_instructions.strip():
        parts.extend(["", "# Additional Instructions", persona.extra_instructions.strip()])
    if persona.allowed_paths:
        paths = ", ".join(persona.allowed_paths)
        parts.extend(["", f"# Scope: only modify paths matching: {paths}"])
    parts.extend(["", "# Task", task.goal.strip()])
    if task.context.strip():
        parts.extend(["", "# Context", task.context.strip()])
    parts.append("")
    parts.append(
        "Execute this task in the workspace. Return a concise summary of what you "
        "did, files changed, and any follow-up needed."
    )
    return "\n".join(parts)


def _build_review_prompt(
    reviewer: Persona,
    task: FleetTask,
    implementation_summary: str,
) -> str:
    body = _read_persona_prompt(reviewer)
    return "\n".join(
        [
            "# Persona",
            body.strip(),
            "",
            "# Original Task",
            task.goal.strip(),
            "",
            "# Implementation Summary",
            implementation_summary.strip(),
            "",
            "Review the implementation. List issues by severity (blocker/major/minor), "
            "note missing tests, and give a clear verdict: APPROVE or REQUEST_CHANGES.",
        ]
    )


def run_execute_phase(
    *,
    backend: LLMBackend,
    resolver: YamlPersonaResolver,
    task: FleetTask,
    workspace: Path,
    timeout_s: int,
) -> dict[str, Any]:
    persona = resolver.load(task.persona)
    prompt = _build_execute_prompt(persona, task)
    result = backend.run(
        prompt,
        max_tokens=0,
        timeout_s=timeout_s,
        cwd=workspace,
        model=persona.model,
        mode=parse_agent_mode(persona.mode),
    )
    return {
        "phase": "execute",
        "persona": persona.name,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
        "duration_s": result.duration_s,
        "agent_id": result.agent_id,
    }


def run_review_phase(
    *,
    backend: LLMBackend,
    resolver: YamlPersonaResolver,
    task: FleetTask,
    workspace: Path,
    timeout_s: int,
    implementation_summary: str,
    reviewer_persona: str = "reviewer",
) -> dict[str, Any]:
    persona = resolver.load(reviewer_persona)
    prompt = _build_review_prompt(persona, task, implementation_summary)
    result = backend.run(
        prompt,
        max_tokens=0,
        timeout_s=timeout_s,
        cwd=workspace,
        model=persona.model,
        mode=parse_agent_mode(persona.mode),
    )
    return {
        "phase": "review",
        "persona": persona.name,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
        "duration_s": result.duration_s,
        "agent_id": result.agent_id,
    }


def run_pipeline(
    *,
    backend: LLMBackend,
    resolver: YamlPersonaResolver,
    task: FleetTask,
    workspace: Path,
    timeout_s: int,
    phases: list[str],
    reviewer_persona: str = "reviewer",
) -> tuple[list[dict[str, Any]], str, int]:
    """Run ordered phases. Returns (phase_results, final_summary, exit_code)."""
    results: list[dict[str, Any]] = []
    summary = ""
    exit_code = 0

    for phase in phases:
        if phase == "execute":
            phase_result = run_execute_phase(
                backend=backend,
                resolver=resolver,
                task=task,
                workspace=workspace,
                timeout_s=timeout_s,
            )
            results.append(phase_result)
            summary = phase_result["stdout"]
            exit_code = phase_result["exit_code"]
            if exit_code != 0:
                break
        elif phase == "review":
            if not summary.strip():
                summary = "(no implementation output to review)"
            phase_result = run_review_phase(
                backend=backend,
                resolver=resolver,
                task=task,
                workspace=workspace,
                timeout_s=timeout_s,
                implementation_summary=summary,
                reviewer_persona=reviewer_persona,
            )
            results.append(phase_result)
            summary = phase_result["stdout"]
            exit_code = phase_result["exit_code"]
            if exit_code != 0:
                break
        else:
            results.append({"phase": phase, "error": f"Unknown phase: {phase}"})
            exit_code = 1
            break

    return results, summary, exit_code
