"""Phase runners for coding fleet pipelines."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_fleet.agent_mode import parse_agent_mode
from agent_fleet.contracts.review import ReviewVerdict
from agent_fleet.personas import read_persona_body
from agent_fleet.pr_review.runner import run_pr_review
from agent_fleet.prompts.agent import build_agent_prompt
from agent_fleet.reviewer import aggregate_verdict
from agent_fleet.reviewer import review as structured_review
from agent_fleet.scope import files_outside_allowed_paths
from agent_fleet.skills_lib import base_kit_skill_dirs, resolve_skill_path
from agent_fleet.verify_core import (
    get_working_tree_changes,
    get_working_tree_diff,
    run_shell_verify,
)

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.hooks import FleetTask, LLMBackend, LLMSession, Persona, PersonaResolver
    from agent_fleet.repo import RepoConfig


def _review_skill_prompt_append(task: FleetTask) -> str:
    if task.equip is None or not task.equip.skill_slots_review:
        return ""
    blocks: list[str] = []
    for skill_id in task.equip.skill_slots_review:
        path = resolve_skill_path(skill_id, base_kit_skill_dirs())
        if path is not None:
            blocks.append(path.read_text(encoding="utf-8").strip())
    if not blocks:
        return ""
    return "\n\n".join(["# Review Skills", *blocks])


def _build_execute_prompt(persona: Persona, task: FleetTask) -> str:
    if task.equip is not None and task.equip.compose_body.strip():
        body = task.equip.compose_body.strip()
    else:
        body = read_persona_body(persona)
    return build_agent_prompt(
        persona_body=body,
        task_heading="Task",
        task_body=task.goal,
        context=task.context,
        extra_instructions=persona.extra_instructions,
        allowed_paths=persona.allowed_paths,
        closing_instruction=(
            "Execute this task in the workspace. Return a concise summary of what you "
            "did, files changed, and any follow-up needed."
        ),
    ).full


def run_execute_phase(
    *,
    backend: LLMBackend,
    resolver: PersonaResolver,
    task: FleetTask,
    workspace: Path,
    timeout_s: int,
    session: LLMSession | None = None,
) -> dict[str, Any]:
    persona = resolver.load(task.persona)
    prompt = _build_execute_prompt(persona, task)
    if session is not None:
        result = session.send(
            prompt,
            max_tokens=0,
            timeout_s=timeout_s,
            allowed_tools=list(persona.allowed_tools),
        )
    else:
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
        "phase": "execute",
        "persona": persona.name,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
        "duration_s": result.duration_s,
        "agent_id": result.agent_id,
    }


def run_scope_phase(
    *,
    persona: Persona,
    changed_files: list[str],
) -> dict[str, Any]:
    violating = files_outside_allowed_paths(persona.allowed_paths, changed_files)
    return {
        "phase": "scope",
        "persona": persona.name,
        "changed_files": changed_files,
        "allowed_paths": list(persona.allowed_paths),
        "violating_files": list(violating),
        "passed": not violating,
        "exit_code": 0 if not violating else 1,
    }


def run_verify_phases(
    *,
    workspace: Path,
    repo: RepoConfig | None,
    timeout_s: int,
) -> list[dict[str, Any]]:
    if repo is None or not repo.verify_commands:
        return []

    results: list[dict[str, Any]] = []
    for command in repo.verify_commands:
        outcome = run_shell_verify(workspace, command, timeout_s=timeout_s)
        results.append({"phase": "verify", **outcome})
        if not outcome["passed"]:
            break
    return results


def run_pr_analyzer_review_phase(
    *,
    backend: LLMBackend,
    resolver: PersonaResolver,
    task: FleetTask,
    workspace: Path,
    timeout_s: int,
    changed_files: list[str],
    implementation_summary: str,
    repo: RepoConfig | None,
    reviewer_persona: str = "pr-analyzer",
) -> dict[str, Any]:
    del resolver, task, timeout_s, changed_files, implementation_summary
    pr_config = repo.pr_review if repo and repo.pr_review else None
    if pr_config is None or not pr_config.enabled:
        return {
            "phase": "review",
            "persona": reviewer_persona,
            "error": "pr_review not configured in .agent-fleet.yaml",
            "passed": False,
            "exit_code": 1,
        }

    base_branch = repo.default_branch if repo else "main"
    result = run_pr_review(
        workspace=workspace,
        base_branch=base_branch,
        backend=backend,
    )
    analysis = result["analysis"]
    verdict = str(result["verdict"])
    passed = verdict == ReviewVerdict.APPROVE.value
    return {
        "phase": "review",
        "persona": pr_config.reviewer_persona,
        "verdict": verdict,
        "risk_level": result.get("risk_level"),
        "analysis": analysis,
        "comment_markdown": result.get("comment_markdown"),
        "reviews": [result["review"]],
        "summary": str(analysis.get("summary") or ""),
        "stdout": result.get("comment_markdown") or "",
        "stderr": "",
        "exit_code": 0 if passed else 1,
        "passed": passed,
    }


def run_analyze_phase(
    *,
    backend: LLMBackend,
    workspace: Path,
    repo: RepoConfig | None,
    pr_number: int = 0,
) -> dict[str, Any]:
    base_branch = repo.default_branch if repo else "main"
    result = run_pr_review(
        workspace=workspace,
        base_branch=base_branch,
        backend=backend,
        pr_number=pr_number,
    )
    verdict = str(result["verdict"])
    passed = verdict == ReviewVerdict.APPROVE.value
    return {
        "phase": "analyze",
        "verdict": verdict,
        "risk_level": result.get("risk_level"),
        "analysis": result["analysis"],
        "comment_markdown": result.get("comment_markdown"),
        "changed_files": result.get("changed_files"),
        "summary": str(result["analysis"].get("summary") or ""),
        "stdout": result.get("comment_markdown") or "",
        "stderr": "",
        "exit_code": 0 if passed else 1,
        "passed": passed,
    }


def run_structured_review_phase(
    *,
    backend: LLMBackend,
    resolver: PersonaResolver,
    task: FleetTask,
    workspace: Path,
    timeout_s: int,
    changed_files: list[str],
    implementation_summary: str,
    reviewer_persona: str = "reviewer",
    repo: RepoConfig | None = None,
) -> dict[str, Any]:
    pr_config = repo.pr_review if repo and repo.pr_review else None
    if pr_config and pr_config.enabled and pr_config.use_in_code_review:
        return run_pr_analyzer_review_phase(
            backend=backend,
            resolver=resolver,
            task=task,
            workspace=workspace,
            timeout_s=timeout_s,
            changed_files=changed_files,
            implementation_summary=implementation_summary,
            repo=repo,
            reviewer_persona=pr_config.reviewer_persona,
        )

    reviewer = resolver.load(reviewer_persona)
    pr_diff = get_working_tree_diff(workspace)
    review_context = task.context
    skill_append = _review_skill_prompt_append(task)
    if skill_append:
        review_context = (
            f"{skill_append}\n\n{review_context}".strip()
            if review_context.strip()
            else skill_append
        )
    try:
        reviews = structured_review(
            1,
            pr_diff,
            changed_files or ["(no files changed)"],
            backend=backend,
            timeout_s=timeout_s,
            cwd=workspace,
            task_goal=task.goal,
            task_context=review_context,
            implementation_summary=implementation_summary,
            model=reviewer.model,
            allowed_tools=list(reviewer.allowed_tools),
        )
        verdict = aggregate_verdict(reviews)
        passed = verdict == ReviewVerdict.APPROVE
        return {
            "phase": "review",
            "persona": reviewer.name,
            "verdict": verdict.value,
            "reviews": [review.to_dict() for review in reviews],
            "summary": reviews[-1].summary if reviews else "",
            "stdout": reviews[-1].summary if reviews else "",
            "stderr": "",
            "exit_code": 0 if passed else 1,
            "passed": passed,
        }
    except Exception as exc:
        return {
            "phase": "review",
            "persona": reviewer.name,
            "verdict": ReviewVerdict.REQUEST_CHANGES.value,
            "error": str(exc),
            "stdout": "",
            "stderr": str(exc),
            "exit_code": 1,
            "passed": False,
        }


def resolve_pipeline_outcome(
    phase_results: list[dict[str, Any]],
    exit_code: int,
) -> tuple[str, str | None]:
    """Map phase results to fleet status and error message."""
    if exit_code == 0:
        return "completed", None

    by_phase = {str(item.get("phase")): item for item in phase_results}
    scope = by_phase.get("scope")
    if scope and not scope.get("passed", True):
        violating = scope.get("violating_files") or []
        return "scope_violation", f"Files outside persona scope: {violating}"

    for verify in [item for item in phase_results if item.get("phase") == "verify"]:
        if not verify.get("passed", True):
            command = verify.get("command", "verify")
            return "verify_failed", f"Verify command failed: {command}"

    analyze = by_phase.get("analyze")
    if analyze and not analyze.get("passed", True):
        verdict = analyze.get("verdict", "request_changes")
        return "review_changes_requested", f"PR analyzer verdict: {verdict}"

    review = by_phase.get("review")
    if review:
        verdict = review.get("verdict")
        if verdict == ReviewVerdict.BLOCK.value:
            return "review_blocked", review.get("summary") or "Reviewer blocked merge"
        if verdict == ReviewVerdict.REQUEST_CHANGES.value:
            return "review_changes_requested", review.get("summary") or "Reviewer requested changes"

    execute = by_phase.get("execute")
    if execute and execute.get("exit_code"):
        return "error", execute.get("stderr") or execute.get("error") or "Implementer failed"

    last = phase_results[-1] if phase_results else {}
    return "error", last.get("stderr") or last.get("error") or "Fleet pipeline failed"


def collect_changed_files(workspace: Path) -> list[str]:
    return get_working_tree_changes(workspace)


def run_pipeline(
    *,
    backend: LLMBackend,
    resolver: PersonaResolver,
    task: FleetTask,
    workspace: Path,
    timeout_s: int,
    phases: list[str],
    reviewer_persona: str = "reviewer",
    repo: RepoConfig | None = None,
    session: LLMSession | None = None,
) -> tuple[list[dict[str, Any]], str, int, list[str]]:
    """Run ordered phases.

    Returns (phase_results, final_summary, exit_code, changed_files).
    """
    results: list[dict[str, Any]] = []
    summary = ""
    exit_code = 0
    changed_files: list[str] = []
    implementer_persona = resolver.load(task.persona)
    use_hardened_review = "review" in phases

    for phase in phases:
        if phase == "execute":
            phase_result = run_execute_phase(
                backend=backend,
                resolver=resolver,
                task=task,
                workspace=workspace,
                timeout_s=timeout_s,
                session=session,
            )
            results.append(phase_result)
            summary = phase_result["stdout"]
            exit_code = phase_result["exit_code"]
            changed_files = collect_changed_files(workspace)
            if exit_code != 0:
                break

            if use_hardened_review:
                scope_result = run_scope_phase(
                    persona=implementer_persona,
                    changed_files=changed_files,
                )
                results.append(scope_result)
                if scope_result["exit_code"] != 0:
                    exit_code = 1
                    summary = f"Scope violation: {scope_result['violating_files']}"
                    break

                verify_results = run_verify_phases(
                    workspace=workspace,
                    repo=repo,
                    timeout_s=timeout_s,
                )
                results.extend(verify_results)
                if verify_results and not verify_results[-1]["passed"]:
                    exit_code = 1
                    summary = verify_results[-1].get("detail") or "Verify failed"
                    break
            continue

        if phase == "analyze":
            phase_result = run_analyze_phase(
                backend=backend,
                workspace=workspace,
                repo=repo,
            )
            results.append(phase_result)
            summary = phase_result.get("summary") or summary
            exit_code = phase_result["exit_code"]
            changed_files = phase_result.get("changed_files") or changed_files
            continue

        if phase == "review":
            if use_hardened_review:
                phase_result = run_structured_review_phase(
                    backend=backend,
                    resolver=resolver,
                    task=task,
                    workspace=workspace,
                    timeout_s=timeout_s,
                    changed_files=changed_files,
                    implementation_summary=summary,
                    reviewer_persona=reviewer_persona,
                    repo=repo,
                )
            else:
                phase_result = _legacy_review_phase(
                    backend=backend,
                    resolver=resolver,
                    task=task,
                    workspace=workspace,
                    timeout_s=timeout_s,
                    implementation_summary=summary,
                    reviewer_persona=reviewer_persona,
                    session=session,
                )
            results.append(phase_result)
            summary = phase_result.get("summary") or phase_result.get("stdout") or summary
            exit_code = phase_result["exit_code"]
            if exit_code != 0:
                break
            continue

        results.append({"phase": phase, "error": f"Unknown phase: {phase}"})
        exit_code = 1
        break

    return results, summary, exit_code, changed_files


def _legacy_review_phase(
    *,
    backend: LLMBackend,
    resolver: PersonaResolver,
    task: FleetTask,
    workspace: Path,
    timeout_s: int,
    implementation_summary: str,
    reviewer_persona: str,
    session: LLMSession | None = None,
) -> dict[str, Any]:
    persona = resolver.load(reviewer_persona)
    body = read_persona_body(persona)
    prompt_parts = [
        "# Persona",
        body.strip(),
    ]
    skill_append = _review_skill_prompt_append(task)
    if skill_append:
        prompt_parts.extend(["", skill_append])
    prompt_parts.extend(
        [
            "",
            "# Original Task",
            task.goal.strip(),
            "",
            "# Implementation Summary",
            implementation_summary.strip() or "(no implementation output to review)",
            "",
            "Review the implementation. List issues by severity (blocker/major/minor), "
            "note missing tests, and give a clear verdict: APPROVE or REQUEST_CHANGES.",
        ]
    )
    prompt = "\n".join(prompt_parts)
    if session is not None:
        result = session.send(
            prompt,
            max_tokens=0,
            timeout_s=timeout_s,
            allowed_tools=list(persona.allowed_tools),
        )
    else:
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
        "phase": "review",
        "persona": persona.name,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
        "duration_s": result.duration_s,
        "agent_id": result.agent_id,
    }
