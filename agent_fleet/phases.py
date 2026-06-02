"""Phase runners for coding fleet pipelines."""

from __future__ import annotations

import logging
import shlex
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from agent_fleet.agent_mode import parse_agent_mode
from agent_fleet.complexity import observe_token_ceiling
from agent_fleet.contracts.review import ReviewVerdict
from agent_fleet.observability.context import bind_phase, get_run_log
from agent_fleet.observability.efficiency import changed_lines
from agent_fleet.personas import read_persona_body
from agent_fleet.pr_review.runner import run_pr_review
from agent_fleet.prompts.agent import build_agent_prompt
from agent_fleet.reviewer import aggregate_verdict
from agent_fleet.reviewer import review as structured_review
from agent_fleet.scope import effective_allowed_paths, files_outside_allowed_paths
from agent_fleet.skills_lib import base_kit_skill_dirs, resolve_skill_path
from agent_fleet.verify_core import (
    get_working_tree_changes,
    get_working_tree_diff,
    revert_paths,
    run_shell_verify,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.config import FleetConfig
    from agent_fleet.hooks import FleetTask, LLMBackend, LLMSession, Persona, PersonaResolver
    from agent_fleet.level_up.models import DispatchEquip
    from agent_fleet.repo import RepoConfig

# The phase names run_pipeline knows how to dispatch.  validate_phases rejects
# anything outside this set before any agent runs.
_KNOWN_PHASES: frozenset[str] = frozenset({"execute", "analyze", "review"})


def validate_phases(phases: list[str]) -> None:
    """Raise ValueError if any phase name is not a known phase.

    Call at pipeline-resolve time so unknown names fail before any agent runs.
    """
    unknown = [p for p in phases if p not in _KNOWN_PHASES]
    if unknown:
        known = sorted(_KNOWN_PHASES)
        raise ValueError(f"Unknown phase(s) {unknown!r}; known phases are {known}")


def _record_token_ceiling_metric(
    *,
    token_ceiling: int,
    declared_complexity: str,
) -> dict[str, Any] | None:
    """Emit ``complexity.ceiling_metric`` when over budget; never aborts the pipeline."""
    breach = observe_token_ceiling(
        token_ceiling=token_ceiling,
        declared_complexity=declared_complexity,
    )
    if breach is None:
        return None
    run_log = get_run_log()
    if run_log is not None:
        run_log.emit("complexity.ceiling_metric", data=breach.to_dict())
    logger.warning(
        "Token ceiling metric: %s tokens > %s ceiling (complexity=%s); continuing pipeline",
        breach.observed_total_tokens,
        breach.ceiling,
        breach.declared_complexity,
    )
    return {"phase": "complexity", "metric_only": True, **breach.to_dict()}


def _review_skill_prompt_append(task: FleetTask) -> str:
    if task.equip is None or not task.equip.skill_slots_review:
        return ""
    return _review_skills_from_slots(task.equip.skill_slots_review)


def _review_skills_from_slots(skill_slots_review: tuple[str, ...] | list[str]) -> str:
    if not skill_slots_review:
        return ""
    blocks: list[str] = []
    for skill_id in skill_slots_review:
        path = resolve_skill_path(skill_id, base_kit_skill_dirs())
        if path is not None:
            blocks.append(path.read_text(encoding="utf-8").strip())
    if not blocks:
        return ""
    return "\n\n".join(["# Review Skills", *blocks])


def _resolve_persona_equip(
    *,
    persona_name: str,
    task: FleetTask,
    fleet_config: FleetConfig | None,
    repo: RepoConfig | None,
) -> DispatchEquip | None:
    if fleet_config is None:
        return None
    from agent_fleet.orchestration.equip import resolve_dispatch_equip

    equipped_task = replace(task, persona=persona_name)
    return resolve_dispatch_equip(equipped_task, fleet_config, repo)


def _equipped_persona_body(
    *,
    persona_name: str,
    task: FleetTask,
    persona: Persona,
    fleet_config: FleetConfig | None,
    repo: RepoConfig | None,
) -> str:
    equip = _resolve_persona_equip(
        persona_name=persona_name,
        task=task,
        fleet_config=fleet_config,
        repo=repo,
    )
    if equip is not None and equip.compose_body.strip():
        return equip.compose_body.strip()
    return read_persona_body(persona)


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
        allowed_paths=effective_allowed_paths(task.allowed_paths, persona.allowed_paths),
        closing_instruction=(
            "Execute this task in the workspace. Return a concise summary of what you "
            "did, files changed, and any follow-up needed."
        ),
    ).full


def _build_legacy_review_prompt(
    persona: Persona,
    task: FleetTask,
    implementation_summary: str,
) -> str:
    review_context = task.context
    skill_append = _review_skill_prompt_append(task)
    if skill_append:
        review_context = (
            f"{skill_append}\n\n{review_context}".strip()
            if review_context.strip()
            else skill_append
        )
    return build_agent_prompt(
        persona_body=read_persona_body(persona),
        task_heading="Original Task",
        task_body=task.goal,
        context=review_context,
        extra_instructions=persona.extra_instructions,
        allowed_paths=effective_allowed_paths(task.allowed_paths, persona.allowed_paths),
        extra_sections=[
            (
                "Implementation Summary",
                implementation_summary.strip() or "(no implementation output to review)",
            ),
        ],
        closing_instruction=(
            "Review the implementation. List issues by severity (blocker/major/minor), "
            "note missing tests, and give a clear verdict: APPROVE or REQUEST_CHANGES."
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
    _persist_execute_artifact(
        persona=persona.name,
        task=task,
        workspace=workspace,
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        agent_id=result.agent_id,
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


def _persist_execute_artifact(
    *,
    persona: str,
    task: FleetTask,
    workspace: Path,
    stdout: str,
    stderr: str,
    exit_code: int,
    agent_id: str | None,
) -> None:
    """Write execute-phase output to ~/.agent-fleet/runs/ so deliverables survive worktree teardown.

    Also snapshots uncommitted/untracked files from the worktree so a future
    redispatch can resume from where the agent left off even if the worktree
    is wiped out-of-band (e.g. cursor-sdk bash subprocess churn).
    """
    import json
    import logging
    import shutil
    import subprocess
    import time
    from pathlib import Path as _Path

    log = logging.getLogger(__name__)

    try:
        runs_root = _Path.home() / ".agent-fleet" / "runs"
        run_id = agent_id or f"run-{int(time.time())}"
        target = runs_root / run_id
        target.mkdir(parents=True, exist_ok=True)
        (target / "stdout.md").write_text(stdout or "", encoding="utf-8")
        if stderr:
            (target / "stderr.txt").write_text(stderr, encoding="utf-8")
        meta = {
            "persona": persona,
            "goal": task.goal,
            "workspace": str(workspace),
            "exit_code": exit_code,
            "agent_id": agent_id,
            "ts": time.time(),
        }
        (target / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except Exception:
        logging.getLogger(__name__).exception("persist_execute_artifact failed")
        return

    # Best-effort filesystem snapshot of the worktree state. Failures here
    # must never break the run — stdout/meta have already been persisted.
    try:
        if not workspace.exists() or not (workspace / ".git").exists():
            return

        def _git(args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", *args],
                cwd=str(workspace),
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )

        status = _git(["status", "--porcelain", "-z"])
        if status.returncode != 0 or not status.stdout:
            return

        files_dir = target / "files"
        entries: list[dict[str, str]] = []
        # -z output uses NUL separators; rename entries use a double NUL.
        tokens = status.stdout.split("\0")
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if not tok:
                i += 1
                continue
            code = tok[:2]
            path = tok[3:] if len(tok) > 3 else ""
            if code.startswith(("R", "C")):
                # Rename/copy: next -z token is the source path; skip it.
                i += 2
            else:
                i += 1
            if not path:
                continue
            entries.append({"code": code, "path": path})
            src = workspace / path
            if src.is_file():
                dst = files_dir / path
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(src, dst)
                except OSError as exc:
                    log.warning("snapshot copy failed for %s: %s", path, exc)

        if not entries:
            return

        # Capture working-tree diff vs HEAD (covers tracked file edits but not
        # untracked files; the raw file copies above cover those).
        diff = _git(["diff", "HEAD", "--binary"])
        if diff.returncode == 0 and diff.stdout:
            (target / "worktree.patch").write_text(diff.stdout, encoding="utf-8")

        branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])
        head_sha = _git(["rev-parse", "HEAD"])
        snapshot_meta = {
            "ts": time.time(),
            "workspace": str(workspace),
            "branch": branch.stdout.strip() if branch.returncode == 0 else None,
            "head": head_sha.stdout.strip() if head_sha.returncode == 0 else None,
            "entries": entries,
        }
        (target / "snapshot.json").write_text(json.dumps(snapshot_meta, indent=2), encoding="utf-8")
    except Exception:
        log.exception("persist_execute_artifact: snapshot step failed")


def run_scope_phase(
    *,
    persona: Persona,
    changed_files: list[str],
    task: FleetTask | None = None,
) -> dict[str, Any]:
    task_paths = task.allowed_paths if task is not None else ()
    allowed = effective_allowed_paths(task_paths, persona.allowed_paths)
    violating = files_outside_allowed_paths(allowed, changed_files)
    return {
        "phase": "scope",
        "persona": persona.name,
        "changed_files": changed_files,
        "allowed_paths": list(allowed),
        "violating_files": list(violating),
        "passed": not violating,
        "exit_code": 0 if not violating else 1,
    }


def run_verify_phases(
    *,
    workspace: Path,
    repo: RepoConfig | None,
    timeout_s: int,
    persona: str | None = None,
    allowed_paths: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    if repo is None:
        return []
    commands = repo.verify_commands_for(persona)
    if not commands:
        return []

    results: list[dict[str, Any]] = []
    for command in commands:
        outcome = run_scoped_lint_command(
            workspace, command, timeout_s=timeout_s, allowed_paths=allowed_paths
        )
        results.append({"phase": "verify", **outcome})
        if not outcome["passed"]:
            break
    return results


def run_scoped_lint_command(
    workspace: Path,
    command: str,
    *,
    timeout_s: int,
    allowed_paths: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Run one verify command, auto-fixing and re-gating ruff to the task's lane.

    The single objective lint gate shared by dispatch verify and the pr_loop
    commit preflight: a failing ``ruff check`` is auto-fixed once and then
    re-judged only on the task's own in-lane changed files, so pre-existing
    out-of-lane debt never fails the gate. Non-ruff commands run unchanged.
    """
    outcome = run_shell_verify(workspace, command, timeout_s=timeout_s)
    if not outcome["passed"] and "ruff check" in command and "--fix" not in command:
        outcome = _autofix_and_regate_lint(
            workspace=workspace,
            command=command,
            timeout_s=timeout_s,
            allowed_paths=allowed_paths,
        )
    return outcome


def _autofix_and_regate_lint(
    *,
    workspace: Path,
    command: str,
    timeout_s: int,
    allowed_paths: tuple[str, ...],
) -> dict[str, Any]:
    """Auto-fix a failing ruff command, then gate only on files this task changed.

    Two scope guards work together so a scoped task is never failed — or
    flagged for a scope violation — by lint it did not create:

    1. ``ruff --fix`` is applied once (I001, unused imports, etc.), then any
       fix that strayed outside ``allowed_paths`` is reverted, so the auto-fix
       cannot manufacture a scope violation.
    2. The pass/fail decision is re-run against only the task's own changed,
       in-lane ``.py`` files. Pre-existing debt elsewhere in the lane (or repo)
       is invisible to the gate.
    """
    before = set(get_working_tree_changes(workspace))
    run_shell_verify(workspace, f"{command} --fix", timeout_s=timeout_s)
    if allowed_paths:
        fixed = sorted(set(get_working_tree_changes(workspace)) - before)
        revert_paths(workspace, files_outside_allowed_paths(allowed_paths, fixed))

    changed = get_working_tree_changes(workspace)
    out_of_lane = (
        set(files_outside_allowed_paths(allowed_paths, changed)) if allowed_paths else set()
    )
    targets = [
        f
        for f in changed
        if f not in out_of_lane and f.endswith(".py") and (workspace / f).exists()
    ]
    if not targets:
        return run_shell_verify(workspace, command, timeout_s=timeout_s)

    head = command.split(" check ", 1)[0] + " check"
    diff_command = f"{head} {' '.join(shlex.quote(t) for t in targets)}"
    return run_shell_verify(workspace, diff_command, timeout_s=timeout_s)


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

    ceiling = by_phase.get("ceiling_abort")
    if ceiling and ceiling.get("enforced"):
        observed = ceiling.get("observed_total_tokens")
        limit = ceiling.get("ceiling")
        return "token_ceiling_exceeded", f"Token ceiling exceeded: {observed} > {limit}"

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


REVIEW_SKIP_LINES_THRESHOLD: int = 50


def review_skip_reason(*, n_changed: int, verify_results: list[dict[str, Any]]) -> str | None:
    """Reason to skip the advisory review, or None to run it.

    Skip only when an objective verify gate ran green and the changed-line delta
    is below the threshold. With no verify gate there is nothing green to lean
    on, so the review runs. Shared by the first pass (run_pipeline) and the
    post-fix re-review so both gate identically.
    """
    if not (verify_results and verify_results[-1].get("passed")):
        return None
    if n_changed >= REVIEW_SKIP_LINES_THRESHOLD:
        return None
    return f"green gates, {n_changed} changed lines < {REVIEW_SKIP_LINES_THRESHOLD}"


def build_review_skip_result(reason: str) -> dict[str, Any]:
    """The synthetic review phase result recorded when the review is gated out."""
    return {
        "phase": "review",
        "verdict": "approve",
        "skipped": True,
        "reason": f"review skipped: {reason}",
        "passed": True,
        "exit_code": 0,
        "summary": f"review skipped: {reason}",
        "stdout": "",
        "stderr": "",
    }


_BOOTSTRAP_VERIFY_SIGNALS: tuple[str, ...] = (
    "conftestimportfailure",
    "errors during collection",
    "error collecting",
    "internalerror",
    "modulenotfounderror",
    "no module named",
    "_prepareconfig",
    "syntaxerror",
)


def _verify_harness_ran(text: str) -> bool:
    """True when the test harness started and reported results.

    pytest prints a ``collected N items`` line and a passed/failed summary only
    after collection succeeds. Their presence means a failure is a genuine test
    failure, not a harness that could not start, so an in-test import error is
    never mistaken for a collection crash.
    """
    return "collected" in text and ("passed" in text or "failed" in text)


def classify_verify_failure(outcome: dict[str, Any]) -> str:
    """Classify a failing verify outcome as ``"test"`` or ``"bootstrap"``.

    A bootstrap error means the verify command could not run: an import,
    collection, syntax, or config crash (e.g. pytest dying in ``_prepareconfig``
    on a ModuleNotFoundError). No fix-loop rewrite recovers it, so the loop must
    not spend full-context fix agents against a harness that cannot start. A
    genuine assertion failure ran the harness and is fixable.
    """
    detail = str(outcome.get("detail") or "")
    text = (detail or f"{outcome.get('stdout', '')}{outcome.get('stderr', '')}").lower()
    if _verify_harness_ran(text):
        return "test"
    return "bootstrap" if any(sig in text for sig in _BOOTSTRAP_VERIFY_SIGNALS) else "test"


def last_verify_failure_is_bootstrap(phase_results: list[dict[str, Any]]) -> bool:
    """True when the most recent failing verify is a broken harness, not a test failure."""
    for item in reversed(phase_results):
        if item.get("phase") == "verify" and not item.get("passed", True):
            return classify_verify_failure(item) == "bootstrap"
    return False


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
    fleet_config: FleetConfig | None = None,
    token_ceiling: int | None = None,
    declared_complexity: str | None = None,
    review_blocking: bool = False,
) -> tuple[list[dict[str, Any]], str, int, list[str]]:
    """Run ordered phases.

    Returns (phase_results, final_summary, exit_code, changed_files).

    If *token_ceiling* is set and cumulative token usage exceeds it after a
    phase, a ``complexity.ceiling_metric`` event is emitted and the pipeline
    continues (verify/review still run when configured).
    """
    validate_phases(phases)

    results: list[dict[str, Any]] = []
    summary = ""
    exit_code = 0
    changed_files: list[str] = []
    verify_results: list[dict[str, Any]] = []
    implementer_persona = resolver.load(task.persona)
    use_hardened_review = "review" in phases
    _complexity_label = declared_complexity or "MED"

    for phase in phases:
        if phase == "execute":
            with bind_phase(phase):
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
            if token_ceiling is not None:
                ceiling_phase = _record_token_ceiling_metric(
                    token_ceiling=token_ceiling, declared_complexity=_complexity_label
                )
                if ceiling_phase is not None:
                    results.append(ceiling_phase)
            if exit_code != 0:
                break

            # Objective gates run for every pipeline, not just the hardened
            # review path: scope keeps the diff in lane, verify is the deterministic
            # quality gate. Subjective review is the only pipeline-gated phase.
            scope_result = run_scope_phase(
                persona=implementer_persona,
                changed_files=changed_files,
                task=task,
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
                persona=implementer_persona.name,
                allowed_paths=effective_allowed_paths(
                    task.allowed_paths, implementer_persona.allowed_paths
                ),
            )
            results.extend(verify_results)
            if verify_results and not verify_results[-1]["passed"]:
                exit_code = 1
                summary = verify_results[-1].get("detail") or "Verify failed"
                break
            continue

        if phase == "analyze":
            with bind_phase(phase):
                phase_result = run_analyze_phase(
                    backend=backend,
                    workspace=workspace,
                    repo=repo,
                )
            results.append(phase_result)
            summary = phase_result.get("summary") or summary
            exit_code = phase_result["exit_code"]
            changed_files = phase_result.get("changed_files") or changed_files
            if token_ceiling is not None:
                ceiling_phase = _record_token_ceiling_metric(
                    token_ceiling=token_ceiling, declared_complexity=_complexity_label
                )
                if ceiling_phase is not None:
                    results.append(ceiling_phase)
            continue

        if phase == "review":
            n_changed = changed_lines(workspace)
            skip_reason = review_skip_reason(n_changed=n_changed, verify_results=verify_results)
            if skip_reason is not None:
                skip_result = build_review_skip_result(skip_reason)
                results.append(skip_result)
                summary = skip_result["summary"]
                run_log = get_run_log()
                if run_log is not None:
                    run_log.emit(
                        "review.gate.skipped",
                        data={
                            "changed_lines": n_changed,
                            "threshold": REVIEW_SKIP_LINES_THRESHOLD,
                            "pass": "first",
                        },
                    )
                continue
            with bind_phase(phase):
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
                        fleet_config=fleet_config,
                        repo=repo,
                    )
            results.append(phase_result)
            summary = phase_result.get("summary") or phase_result.get("stdout") or summary
            if token_ceiling is not None:
                ceiling_phase = _record_token_ceiling_metric(
                    token_ceiling=token_ceiling, declared_complexity=_complexity_label
                )
                if ceiling_phase is not None:
                    results.append(ceiling_phase)
            # Review is advisory by default: the verdict is recorded above for
            # surfacing, but only a blocking review adopts its exit code and
            # halts the pipeline. Objective gates (scope, verify) already ran.
            if review_blocking:
                exit_code = phase_result["exit_code"]
                if exit_code != 0:
                    break
            continue

        # validate_phases() above ensures this is unreachable.
        raise AssertionError(f"unhandled phase {phase!r} slipped past validate_phases")

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
    fleet_config: FleetConfig | None = None,
    repo: RepoConfig | None = None,
) -> dict[str, Any]:
    persona = resolver.load(reviewer_persona)
    reviewer_equip = _resolve_persona_equip(
        persona_name=reviewer_persona,
        task=task,
        fleet_config=fleet_config,
        repo=repo,
    )
    if reviewer_equip is not None:
        body = (
            reviewer_equip.compose_body.strip()
            if reviewer_equip.compose_body.strip()
            else read_persona_body(persona)
        )
        skill_append = _review_skills_from_slots(reviewer_equip.skill_slots_review)
    else:
        body = read_persona_body(persona)
        skill_append = _review_skill_prompt_append(task)
    prompt_parts = [
        "# Persona",
        body.strip(),
    ]
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
