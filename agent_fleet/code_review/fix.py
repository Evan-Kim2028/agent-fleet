"""Fix phase for code_review auto-fix loop."""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING, Any

from agent_fleet.agent_mode import parse_agent_mode

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.config import FleetConfig
    from agent_fleet.hooks import FleetTask, LLMBackend
    from agent_fleet.personas import YamlPersonaResolver
    from agent_fleet.repo import RepoConfig


def _review_feedback(phase_results: list[dict[str, Any]]) -> str:
    review = next(
        (item for item in reversed(phase_results) if item.get("phase") == "review"),
        None,
    )
    if not review:
        return ""

    parts: list[str] = []
    if review.get("comment_markdown"):
        parts.append(str(review["comment_markdown"]))
    elif review.get("stdout"):
        parts.append(str(review["stdout"]))
    if review.get("summary"):
        parts.append(f"\nSummary: {review['summary']}")
    verdict = review.get("verdict")
    if verdict:
        parts.append(f"\nVerdict: {verdict}")
    reviews = review.get("reviews")
    if isinstance(reviews, list):
        for item in reviews:
            if not isinstance(item, dict):
                continue
            issues = item.get("issues")
            if isinstance(issues, list):
                for issue in issues:
                    if isinstance(issue, dict):
                        parts.append(
                            f"- [{issue.get('severity', 'info')}] "
                            f"{issue.get('file', '')}: {issue.get('message', '')}"
                        )
    return "\n".join(parts).strip()


def _verify_feedback(phase_results: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in phase_results:
        if item.get("phase") != "verify" or item.get("passed", True):
            continue
        command = item.get("command", "verify")
        detail = item.get("detail") or item.get("stderr") or item.get("stdout") or "failed"
        lines.append(f"Command `{command}` failed:\n{detail}")
    return "\n\n".join(lines)


def run_fix_phase(
    *,
    backend: LLMBackend,
    resolver: YamlPersonaResolver,
    task: FleetTask,
    workspace: Path,
    timeout_s: int,
    phase_results: list[dict[str, Any]],
    repo: RepoConfig | None,
    fix_persona: str,
    attempt: int,
    fleet_config: FleetConfig | None = None,
) -> dict[str, Any]:
    """Dispatch fix persona to address review or verify failures."""
    from dataclasses import replace

    from agent_fleet.config import load_fleet_config
    from agent_fleet.orchestration.equip import resolve_dispatch_equip

    persona = resolver.load(fix_persona)
    review_block = _review_feedback(phase_results)
    verify_block = _verify_feedback(phase_results)

    fc = fleet_config or load_fleet_config()
    fix_task = replace(task, persona=fix_persona)
    equip = resolve_dispatch_equip(fix_task, fc, repo)
    persona_block = ""
    if equip.compose_body.strip():
        persona_block = f"## Equipped Persona\n\n{equip.compose_body.strip()}\n\n"

    verify_commands = ""
    if repo and repo.verify_commands:
        verify_commands = "\n".join(f"- `{cmd}`" for cmd in repo.verify_commands)

    prompt = textwrap.dedent(f"""\
        {persona_block}You are fixing issues found during an automated code review pipeline.

        ## Original task
        {task.goal.strip()}

        ## Context
        {task.context.strip() or "(none)"}

        ## Review feedback
        {review_block or "(none — focus on verify failures below)"}

        ## Verify failures
        {verify_block or "(none)"}

        ## Instructions
        1. Fix every valid blocking issue above with minimal diffs.
        2. Do NOT commit or push — the orchestrator handles git.
        3. Run these verify commands before finishing:
        {verify_commands or "- (none configured)"}
        4. Fix attempt {attempt} — address root causes, not symptoms.
    """)

    result = backend.run(
        prompt,
        max_tokens=0,
        timeout_s=timeout_s,
        cwd=workspace,
        model=persona.model,
        mode=parse_agent_mode(persona.mode),
        allowed_tools=list(persona.allowed_tools),
    )
    return {
        "phase": "fix",
        "persona": persona.name,
        "attempt": attempt,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
        "duration_s": result.duration_s,
        "agent_id": result.agent_id,
    }
