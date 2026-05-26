"""Fix phase for code_review auto-fix loop."""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING, Any

from agent_fleet.agent_mode import parse_agent_mode
from agent_fleet.config import load_fleet_config
from agent_fleet.hooks import FleetTask
from agent_fleet.orchestration.equip import resolve_dispatch_equip
from agent_fleet.prompts.agent import build_agent_prompt
from agent_fleet.repo import merge_repo_into_fleet_config

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.hooks import LLMBackend
    from agent_fleet.level_up.models import DispatchEquip
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


def _resolve_fix_equip(
    *,
    task: FleetTask,
    fix_persona: str,
    repo: RepoConfig | None,
    attempt: int,
) -> DispatchEquip:
    if fix_persona == task.persona and task.equip is not None:
        return task.equip

    fleet_config = load_fleet_config()
    if repo is not None:
        fleet_config = merge_repo_into_fleet_config(fleet_config, repo)

    parent_run_id = task.equip.parent_run_id if task.equip else None
    fix_task = FleetTask(
        goal=task.goal,
        context=task.context,
        persona=fix_persona,
        workspace=task.workspace,
        pipeline=task.pipeline,
        title=task.title,
        equip=task.equip,
    )
    run_id = f"{parent_run_id}-fix-{attempt}" if parent_run_id else f"code-review-fix-{attempt}"
    return resolve_dispatch_equip(fix_task, fleet_config, repo, run_id=run_id)


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
) -> dict[str, Any]:
    """Dispatch fix persona to address review or verify failures."""
    persona = resolver.load(fix_persona)
    review_block = _review_feedback(phase_results)
    verify_block = _verify_feedback(phase_results)
    equip = _resolve_fix_equip(
        task=task,
        fix_persona=fix_persona,
        repo=repo,
        attempt=attempt,
    )

    verify_commands = ""
    if repo and repo.verify_commands:
        verify_commands = "\n".join(f"- `{cmd}`" for cmd in repo.verify_commands)

    instructions = textwrap.dedent(f"""\
        1. Fix every valid blocking issue above with minimal diffs.
        2. Do NOT commit or push — the orchestrator handles git.
        3. Run these verify commands before finishing:
        {verify_commands or "- (none)"}
        4. Fix attempt {attempt} — address root causes, not symptoms.
    """)

    prompt = build_agent_prompt(
        persona_body=equip.compose_body,
        task_heading="Task",
        task_body="You are fixing issues found during an automated code review pipeline.",
        context=task.context.strip() or "(none)",
        extra_instructions=persona.extra_instructions,
        allowed_paths=persona.allowed_paths,
        extra_sections=[
            ("Original task", task.goal.strip()),
            ("Review feedback", review_block or "(none)"),
            ("Verify failures", verify_block or "(none)"),
            ("Instructions", instructions),
        ],
        closing_instruction=(
            "Apply the fixes in the workspace. Return a concise summary of what you "
            "changed and any verify commands you ran."
        ),
    ).full

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
