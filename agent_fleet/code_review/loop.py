"""Auto-fix loop for code_review pipeline."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_fleet.code_review.fix import run_fix_phase
from agent_fleet.phases import (
    collect_changed_files,
    resolve_pipeline_outcome,
    run_pipeline,
    run_scope_phase,
    run_structured_review_phase,
    run_verify_phases,
)

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.code_review.config import CodeReviewConfig
    from agent_fleet.config import FleetConfig
    from agent_fleet.hooks import FleetTask, LLMBackend
    from agent_fleet.personas import YamlPersonaResolver
    from agent_fleet.repo import RepoConfig


def _rerun_quality_gates(
    *,
    backend: LLMBackend,
    resolver: YamlPersonaResolver,
    task: FleetTask,
    workspace: Path,
    timeout_s: int,
    repo: RepoConfig | None,
    implementation_summary: str,
    reviewer_persona: str = "reviewer",
) -> tuple[list[dict[str, Any]], str, int, list[str]]:
    """Re-run scope, verify, and review after a fix attempt."""
    results: list[dict[str, Any]] = []
    persona = resolver.load(task.persona)
    changed_files = collect_changed_files(workspace)
    summary = implementation_summary

    scope_result = run_scope_phase(persona=persona, changed_files=changed_files, task=task)
    results.append(scope_result)
    if scope_result["exit_code"] != 0:
        return results, summary, 1, changed_files

    verify_results = run_verify_phases(
        workspace=workspace, repo=repo, timeout_s=timeout_s, persona=persona.name
    )
    results.extend(verify_results)
    if verify_results and not verify_results[-1]["passed"]:
        return results, summary, 1, changed_files

    review_result = run_structured_review_phase(
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
    results.append(review_result)
    summary = review_result.get("summary") or review_result.get("stdout") or summary
    exit_code = review_result["exit_code"]
    return results, summary, exit_code, changed_files


def run_code_review_with_auto_fix(
    *,
    backend: LLMBackend,
    resolver: YamlPersonaResolver,
    task: FleetTask,
    workspace: Path,
    timeout_s: int,
    phases: list[str],
    repo: RepoConfig | None,
    config: CodeReviewConfig,
    reviewer_persona: str = "reviewer",
    fleet_config: FleetConfig | None = None,
    max_retries: int | None = None,
    token_ceiling: int | None = None,
    declared_complexity: str | None = None,
) -> tuple[list[dict[str, Any]], str, int, list[str]]:
    """Run code_review pipeline with optional fix → re-check loops."""
    if not config.auto_fix or phases != ["execute", "review"]:
        return run_pipeline(
            backend=backend,
            resolver=resolver,
            task=task,
            workspace=workspace,
            timeout_s=timeout_s,
            phases=phases,
            reviewer_persona=reviewer_persona,
            repo=repo,
            fleet_config=fleet_config,
            token_ceiling=token_ceiling,
            declared_complexity=declared_complexity,
        )

    phase_results, summary, exit_code, changed_files = run_pipeline(
        backend=backend,
        resolver=resolver,
        task=task,
        workspace=workspace,
        timeout_s=timeout_s,
        phases=phases,
        reviewer_persona=reviewer_persona,
        repo=repo,
        fleet_config=fleet_config,
        token_ceiling=token_ceiling,
        declared_complexity=declared_complexity,
    )

    effective_max_fix = max_retries if max_retries is not None else config.max_fix_attempts
    fix_persona = config.fix_persona or "coder"
    for attempt in range(1, effective_max_fix + 1):
        status, _error = resolve_pipeline_outcome(phase_results, exit_code)
        if status == "completed":
            break
        if status not in {"review_changes_requested", "verify_failed"}:
            break

        fix_result = run_fix_phase(
            backend=backend,
            resolver=resolver,
            task=task,
            workspace=workspace,
            timeout_s=timeout_s,
            phase_results=phase_results,
            repo=repo,
            fix_persona=fix_persona,
            attempt=attempt,
            fleet_config=fleet_config,
        )
        phase_results.append(fix_result)
        if fix_result["exit_code"] != 0:
            exit_code = 1
            break

        gate_results, summary, exit_code, changed_files = _rerun_quality_gates(
            backend=backend,
            resolver=resolver,
            task=task,
            workspace=workspace,
            timeout_s=timeout_s,
            repo=repo,
            implementation_summary=summary,
            reviewer_persona=reviewer_persona,
        )
        phase_results.extend(gate_results)

    return phase_results, summary, exit_code, changed_files
