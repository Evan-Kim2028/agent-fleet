"""Local fleet runner — full PLAN→RESEARCH→SYNTHESIZE→IMPLEMENT→VERIFY→REVIEW pipeline."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_fleet.contracts.review import ReviewResult, ReviewVerdict
from agent_fleet.contracts.task_spec import DecompositionDecision, TaskSpec
from agent_fleet.contracts.tech_lead_review import TechLeadReview, TechLeadVerdict
from agent_fleet.contracts.verify_result import VerifySeverity
from agent_fleet.implementer import implement
from agent_fleet.phases import run_pipeline
from agent_fleet.planner import plan
from agent_fleet.repo import RepoConfig, find_repo_config
from agent_fleet.researcher import research_all
from agent_fleet.reviewer import review
from agent_fleet.spine_config import SpineConfig
from agent_fleet.synthesizer import synthesize
from agent_fleet.tech_lead import should_invoke_tech_lead, tech_lead_review
from agent_fleet.verify_core import get_changed_files

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_fleet.config import FleetConfig
    from agent_fleet.hooks import (
        FleetTask,
        GitForge,
        GitOps,
        LLMBackend,
        LLMSession,
        PersonaResolver,
        Verifier,
    )

logger = logging.getLogger(__name__)


def _run_outcome(
    review_results: list[ReviewResult],
    tech_lead: TechLeadReview | None,
) -> str:
    for review_result in review_results:
        if review_result.verdict == ReviewVerdict.BLOCK:
            return "review_blocked"
        if review_result.verdict == ReviewVerdict.REQUEST_CHANGES:
            return "review_changes_requested"
    if tech_lead and tech_lead.verdict in (TechLeadVerdict.BLOCK, TechLeadVerdict.ESCALATE):
        return "tech_lead_blocked"
    return "completed"


@dataclass
class FleetRunConfig:
    max_verify_retries: int = 3
    max_research_workers: int = 4
    memory_limit_parent: str = "4G"
    memory_limit_research: str = "2G"
    create_branch: bool = True
    commit_changes: bool = True
    resume: bool = True
    preserve_worktree_on_failure: bool = True


@dataclass
class FleetRunResult:
    run_id: str
    task_id: int
    persona: str
    outcome: str
    task_spec: dict[str, Any] | None = None
    summary: str | None = None
    changed_files: list[str] = field(default_factory=list)
    reviews: list[dict[str, Any]] = field(default_factory=list)
    tech_lead: dict[str, Any] | None = None
    commit_sha: str | None = None
    branch_name: str | None = None
    pr_number: int | None = None
    phases: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration_seconds: float = 0.0


def _spine_from_repo(repo: RepoConfig | None) -> SpineConfig:
    base = SpineConfig.defaults()
    if repo is None:
        return base

    overrides = dict(repo.spine_overrides or {})
    worktree_base = repo.worktree_base
    if worktree_base is None and overrides.get("worktree_base"):
        worktree_base = Path(str(overrides["worktree_base"])).expanduser()

    return SpineConfig(
        worktree_base=worktree_base or base.worktree_base,
        pr_draft_label=str(overrides.get("pr_draft_label", base.pr_draft_label)),
        pr_ready_label=str(overrides.get("pr_ready_label", base.pr_ready_label)),
        branch_prefix=str(overrides.get("branch_prefix", base.branch_prefix)),
        coop_label_prefix=str(overrides.get("coop_label_prefix", base.coop_label_prefix)),
        coop_parent_label_prefix=str(
            overrides.get("coop_parent_label_prefix", base.coop_parent_label_prefix)
        ),
        persona_scope_allowlist=repo.persona_scope_allowlist or base.persona_scope_allowlist,
        cross_cutting_groups=repo.cross_cutting_groups or base.cross_cutting_groups,
        fleet_critical_prefixes=repo.critical_path_prefixes or base.fleet_critical_prefixes,
        design_review_enabled=bool(
            overrides.get("design_review_enabled", base.design_review_enabled)
        ),
        design_visual_surface_globs=tuple(
            overrides.get("design_visual_surface_globs", base.design_visual_surface_globs)
        ),
        design_score_threshold=int(
            overrides.get("design_score_threshold", base.design_score_threshold)
        ),
        design_executor_key=str(overrides.get("design_executor_key", base.design_executor_key)),
        design_rubric_path=str(overrides.get("design_rubric_path", base.design_rubric_path)),
    )


class LocalFleetRunner:
    """Repo-agnostic fleet pipeline runner."""

    def __init__(
        self,
        *,
        backend: LLMBackend,
        persona_resolver: PersonaResolver,
        git_ops: GitOps,
        verifier: Verifier,
        spine: SpineConfig | None = None,
        config: FleetRunConfig | None = None,
        forge: GitForge | None = None,
    ) -> None:
        self._backend = backend
        self._persona_resolver = persona_resolver
        self._git_ops = git_ops
        self._verifier = verifier
        self._spine = spine or SpineConfig.defaults()
        self._config = config or FleetRunConfig()
        self._forge = forge

    def _pr_labels_for_issue(self, issue_number: int | None, base_labels: list[str]) -> list[str]:
        if self._forge is None or issue_number is None:
            return list(base_labels)
        try:
            issue_labels = self._forge.get_labels(issue_number)
        except Exception:
            return list(base_labels)
        prefix = self._spine.coop_parent_label_prefix + "/"
        propagated = [name for name in issue_labels if name.startswith(prefix)]
        return list(base_labels) + propagated

    def run(
        self,
        *,
        task_id: int,
        title: str,
        body: str,
        persona: str,
        repo_root: Path,
        base_branch: str = "main",
        pr_title: str | None = None,
        pr_body_builder: Callable[[str, str | None], str] | None = None,
        pr_labels: list[str] | None = None,
        issue_number: int | None = None,
    ) -> FleetRunResult:
        start = time.monotonic()
        run_id = str(uuid.uuid4())[:8]
        worktree: Path | None = None
        phases: dict[str, Any] = {}
        task_spec: TaskSpec | None = None
        branch_name = f"{self._spine.branch_prefix}/{persona}/{task_id}-{run_id}"
        notes = None
        brief = None
        resume_mode = False
        result: FleetRunResult | None = None

        try:
            if self._config.resume and hasattr(self._git_ops, "find_resume_branch"):
                resumed = self._git_ops.find_resume_branch(
                    task_id,
                    persona,
                    self._spine.branch_prefix,
                )
                if resumed is not None:
                    branch_name, run_id = resumed
                    worktree = self._git_ops.attach_worktree(branch_name, run_id)
                    resume_mode = True
                    logger.info("[%s] RESUME on %s", run_id, branch_name)
                    phases["RESUME"] = {"branch": branch_name, "worktree": str(worktree)}

            logger.info("[%s] PLAN", run_id)
            task_spec = plan(
                task_id,
                title,
                body,
                backend=self._backend,
                persona_resolver=self._persona_resolver,
                spine_config=self._spine,
            )
            phases["PLAN"] = task_spec.to_dict()

            if task_spec.decomposition_decision == DecompositionDecision.REJECTED:
                result = FleetRunResult(
                    run_id=run_id,
                    task_id=task_id,
                    persona=persona,
                    outcome="rejected",
                    task_spec=task_spec.to_dict(),
                    summary=task_spec.decomposition_reason,
                    phases=phases,
                    duration_seconds=round(time.monotonic() - start, 2),
                )
                return result  # noqa: RET504

            if task_spec.decomposition_decision == DecompositionDecision.DECOMPOSE:
                result = FleetRunResult(
                    run_id=run_id,
                    task_id=task_id,
                    persona=persona,
                    outcome="decompose",
                    task_spec=task_spec.to_dict(),
                    summary=task_spec.decomposition_reason,
                    phases=phases,
                    error="Task requires decomposition — dispatch child tasks separately",
                    duration_seconds=round(time.monotonic() - start, 2),
                )
                return result  # noqa: RET504

            if not resume_mode:
                logger.info("[%s] RESEARCH (%d items)", run_id, len(task_spec.research_plan))
                notes = research_all(
                    task_spec.research_plan,
                    backend=self._backend,
                    memory_limit=self._config.memory_limit_research,
                    max_workers=self._config.max_research_workers,
                    cwd=repo_root,
                )
                phases["RESEARCH"] = [n.to_dict() for n in notes]

                logger.info("[%s] SYNTHESIZE", run_id)
                brief = synthesize(task_spec, notes, backend=self._backend)
                phases["SYNTHESIZE"] = brief.to_dict()

                logger.info("[%s] IMPLEMENT", run_id)
                worktree = self._git_ops.setup_workspace(
                    repo_root,
                    run_id,
                    base_branch,
                    branch_name=branch_name if self._config.create_branch else None,
                )
                if self._config.create_branch and not getattr(self._git_ops, "use_worktree", False):
                    self._git_ops.create_branch(worktree, branch_name)

                implement(
                    brief,
                    task_spec,
                    worktree,
                    branch_name,
                    backend=self._backend,
                    persona_resolver=self._persona_resolver,
                    persona_name=persona,
                    memory_limit=self._config.memory_limit_parent,
                )
                phases["IMPLEMENT"] = {"branch": branch_name, "worktree": str(worktree)}

            assert worktree is not None
            verify_attempts = 0
            verify_result = None
            while verify_attempts <= self._config.max_verify_retries:
                logger.info("[%s] VERIFY (attempt %d)", run_id, verify_attempts + 1)
                changed = self._git_ops.changed_files(worktree)
                verify_result = self._verifier.check(
                    worktree,
                    persona=persona,
                    changed_files=changed,
                    task_id=task_id,
                )
                phases[f"VERIFY_{verify_attempts}"] = verify_result.to_dict()
                if verify_result.severity == VerifySeverity.OK:
                    break
                if verify_result.severity == VerifySeverity.FATAL:
                    break
                verify_attempts += 1
                if verify_attempts > self._config.max_verify_retries:
                    break
                if notes is None:
                    logger.info("[%s] RESEARCH (resume retry)", run_id)
                    notes = research_all(
                        task_spec.research_plan,
                        backend=self._backend,
                        memory_limit=self._config.memory_limit_research,
                        max_workers=self._config.max_research_workers,
                        cwd=repo_root,
                    )
                    phases.setdefault("RESEARCH", [n.to_dict() for n in notes])
                brief = synthesize(
                    task_spec,
                    notes,
                    backend=self._backend,
                    extra_context=f"Verification failed: {verify_result.message}. Fix and retry.",
                )
                implement(
                    brief,
                    task_spec,
                    worktree,
                    branch_name,
                    backend=self._backend,
                    persona_resolver=self._persona_resolver,
                    persona_name=persona,
                    prompt_suffix=f"Previous verify failure: {verify_result.message}",
                )

            if verify_result is None or verify_result.severity != VerifySeverity.OK:
                result = FleetRunResult(
                    run_id=run_id,
                    task_id=task_id,
                    persona=persona,
                    outcome="verify_failed",
                    task_spec=task_spec.to_dict(),
                    changed_files=get_changed_files(worktree),
                    phases=phases,
                    error=verify_result.message if verify_result else "verify failed",
                    duration_seconds=round(time.monotonic() - start, 2),
                )
                return result  # noqa: RET504

            changed_files = get_changed_files(worktree)
            diff = self._git_ops.diff_summary(worktree)

            commit_sha = None
            if self._config.commit_changes:
                commit_sha = self._git_ops.commit_changes(
                    worktree,
                    f"fleet({persona}): {title[:72]}",
                )

            if self._config.commit_changes and commit_sha is None and not changed_files:
                logger.info(
                    "[%s] NOOP: implementer produced no changes; skipping OPEN_PR/REVIEW",
                    run_id,
                )
                phases["NOOP"] = {
                    "reason": "implementer determined no code changes were required",
                }
                if self._forge is not None and (issue_number or task_id):
                    try:
                        self._forge.comment(
                            issue_number or task_id,
                            (
                                f"Fleet run `{run_id}` completed with no code changes — "
                                "the implementer determined the requested work was already "
                                "satisfied by existing code. No PR was opened."
                            ),
                        )
                    except Exception:
                        logger.exception("[%s] NOOP comment failed", run_id)
                result = FleetRunResult(
                    run_id=run_id,
                    task_id=task_id,
                    persona=persona,
                    outcome="completed_noop",
                    task_spec=task_spec.to_dict(),
                    summary=brief.summary if brief else "",
                    changed_files=changed_files,
                    commit_sha=None,
                    branch_name=branch_name,
                    pr_number=None,
                    phases=phases,
                    duration_seconds=round(time.monotonic() - start, 2),
                )
                return result  # noqa: RET504

            pr_number: int | None = None
            if self._forge is not None:
                if brief is None:
                    if notes is None:
                        notes = research_all(
                            task_spec.research_plan,
                            backend=self._backend,
                            memory_limit=self._config.memory_limit_research,
                            max_workers=self._config.max_research_workers,
                            cwd=repo_root,
                        )
                    brief = synthesize(task_spec, notes, backend=self._backend)
                logger.info("[%s] OPEN_PR", run_id)
                self._git_ops.push_branch(worktree, branch_name)
                pr_body = (
                    pr_body_builder(run_id, brief.summary)
                    if pr_body_builder
                    else f"Automated fleet PR. Run: {run_id}\n\nCloses #{task_id}"
                )
                labels = self._pr_labels_for_issue(
                    issue_number or task_id,
                    pr_labels or [self._spine.pr_ready_label],
                )
                pr_number = self._forge.open_pr(
                    title=pr_title or f"{branch_name}",
                    body=pr_body,
                    branch=branch_name,
                    base=base_branch,
                    draft=False,
                    labels=labels,
                )
                phases["OPEN_PR"] = {"pr_number": pr_number, "branch": branch_name}

            logger.info("[%s] REVIEW", run_id)
            review_results = review(
                pr_number or task_id,
                diff,
                changed_files,
                backend=self._backend,
            )
            phases["REVIEW"] = [r.to_dict() for r in review_results]

            tech_lead: TechLeadReview | None = None
            if should_invoke_tech_lead(task_spec, review_results):
                logger.info("[%s] TECH_LEAD", run_id)
                tech_lead = tech_lead_review(
                    task_spec, review_results, pr_number or task_id, backend=self._backend
                )
                if tech_lead:
                    phases["TECH_LEAD"] = tech_lead.to_dict()

            summary_parts = [brief.summary]
            if review_results:
                summary_parts.append(review_results[0].summary)
            summary = "\n\n".join(p for p in summary_parts if p)

            outcome = _run_outcome(review_results, tech_lead)

            result = FleetRunResult(
                run_id=run_id,
                task_id=task_id,
                persona=persona,
                outcome=outcome,
                task_spec=task_spec.to_dict(),
                summary=summary,
                changed_files=changed_files,
                reviews=[r.to_dict() for r in review_results],
                tech_lead=tech_lead.to_dict() if tech_lead else None,
                commit_sha=commit_sha,
                branch_name=branch_name,
                pr_number=pr_number,
                phases=phases,
                duration_seconds=round(time.monotonic() - start, 2),
            )
            return result  # noqa: RET504
        except Exception as exc:
            logger.exception("[%s] fleet run failed", run_id)
            result = FleetRunResult(
                run_id=run_id,
                task_id=task_id,
                persona=persona,
                outcome="error",
                task_spec=task_spec.to_dict() if task_spec else None,
                phases=phases,
                error=str(exc),
                duration_seconds=round(time.monotonic() - start, 2),
            )
            return result  # noqa: RET504
        finally:
            if worktree is not None:
                forensic = self._config.preserve_worktree_on_failure and (
                    result is None
                    or result.outcome
                    in ("verify_failed", "error", "review_blocked", "tech_lead_blocked")
                )
                self._git_ops.teardown_workspace(worktree, forensic=forensic)


class TaskRunner:
    """Thin session-aware runner for a single FleetTask.

    Opens one AgentSession per call to run() when the backend supports
    create_session(), threads it through every pipeline phase, and disposes
    it in a finally block.  Falls back to backend.run() for backends that
    do not expose create_session().
    """

    def __init__(
        self,
        *,
        backend: LLMBackend,
        fleet_config: FleetConfig,
        persona_resolver: PersonaResolver,
        task: FleetTask,
        workspace: Path,
    ) -> None:
        self._backend = backend
        self._fleet_config = fleet_config
        self._persona_resolver = persona_resolver
        self._task = task
        self._workspace = workspace

    def run(self, *, task_id: int, pipeline: str | None = None) -> dict[str, Any]:
        """Run the task through the named pipeline, returning phase results.

        Opens a session via backend.create_session() when available, threads
        it into every phase call, and disposes the session in a finally block.
        """
        pipeline_name = pipeline or self._fleet_config.default_pipeline
        phases = self._fleet_config.pipelines.get(pipeline_name, ["execute"])

        session: LLMSession | None = None
        if hasattr(self._backend, "create_session"):
            persona = self._persona_resolver.load(self._task.persona)
            mcp_specs = {
                name: self._fleet_config.mcp_servers[name]
                for name in (getattr(persona, "mcp_servers", []) or [])
                if name in self._fleet_config.mcp_servers
            }
            session = self._backend.create_session(
                persona_name=self._task.persona,
                cwd=self._workspace,
                mcp_servers=mcp_specs,
                model=persona.model,
                mode=persona.mode,
            )

        try:
            phase_results, summary, exit_code, changed_files = run_pipeline(
                backend=self._backend,
                resolver=self._persona_resolver,  # type: ignore[arg-type]
                task=self._task,
                workspace=self._workspace,
                timeout_s=self._fleet_config.timeout_seconds,
                phases=phases,
                session=session,
            )
            return {
                "task_id": task_id,
                "pipeline": pipeline_name,
                "summary": summary,
                "exit_code": exit_code,
                "changed_files": changed_files,
                "phases": phase_results,
            }
        finally:
            if session is not None:
                session.dispose()


def run_full_pipeline(
    *,
    goal: str,
    context: str = "",
    title: str | None = None,
    persona: str = "coder",
    workspace: Path | str | None = None,
    task_id: int | None = None,
    backend: LLMBackend,
    persona_resolver: PersonaResolver,
) -> FleetRunResult:
    """Convenience entry: discover repo config and run full pipeline."""
    from agent_fleet.integrations.command_verifier import CommandVerifier
    from agent_fleet.integrations.local_git import git_ops_from_repo

    ws = Path(workspace or Path.cwd()).resolve()
    repo = find_repo_config(ws) or RepoConfig(repo_root=ws)
    if workspace:
        repo.repo_root = ws
    runner = LocalFleetRunner(
        backend=backend,
        persona_resolver=persona_resolver,
        git_ops=git_ops_from_repo(repo),
        verifier=CommandVerifier(repo),
        spine=_spine_from_repo(repo),
    )
    body = goal if not context else f"{goal}\n\n## Context\n{context}"
    return runner.run(
        task_id=task_id or int(time.time()) % 100000,
        title=title or goal[:120],
        body=body,
        persona=persona or repo.default_persona,
        repo_root=ws,
        base_branch=repo.default_branch,
    )
