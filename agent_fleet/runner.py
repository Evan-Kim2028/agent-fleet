"""Local fleet runner — full PLAN→RESEARCH→SYNTHESIZE→IMPLEMENT→VERIFY→REVIEW pipeline."""

from __future__ import annotations

import contextlib
import logging
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_fleet.capacity import is_visual_audit_dispatch
from agent_fleet.complexity import derive_runtime, is_actionable_stderr
from agent_fleet.config import load_fleet_config
from agent_fleet.contracts.review import ReviewResult, ReviewVerdict
from agent_fleet.contracts.task_spec import DecompositionDecision, TaskSpec
from agent_fleet.contracts.tech_lead_review import TechLeadReview, TechLeadVerdict
from agent_fleet.contracts.verify_result import VerifySeverity
from agent_fleet.fleet_session import create_fleet_session
from agent_fleet.hooks import FleetTask, ResumableGitOps
from agent_fleet.implementer import implement
from agent_fleet.level_up.paths import repo_key as level_up_repo_key
from agent_fleet.level_up.record import record_runner_experience, review_verdict_from_runner_result
from agent_fleet.observability.context import bind_run, get_run_log
from agent_fleet.observability.efficiency import changed_lines as _changed_lines
from agent_fleet.observability.log import RunLog
from agent_fleet.observability.run_metrics import build_run_metrics
from agent_fleet.orchestration.decompose import coerce_empty_decompose
from agent_fleet.orchestration.equip import resolve_dispatch_equip
from agent_fleet.phase_graph import (
    PhaseGraph,
    PhaseRunContext,
    default_phase_graph,
    should_run_phase,
)
from agent_fleet.phases import run_pipeline
from agent_fleet.planner import plan
from agent_fleet.repo import RepoConfig, find_repo_config
from agent_fleet.researcher import research_all
from agent_fleet.reviewer import review
from agent_fleet.spine_config import SpineConfig
from agent_fleet.synthesizer import synthesize
from agent_fleet.workstreams.scope import path_under_allowlist
from agent_fleet.tech_lead import tech_lead_review
from agent_fleet.verify_core import get_changed_files

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_fleet.config import FleetConfig
    from agent_fleet.hooks import (
        GitForge,
        GitOps,
        LLMBackend,
        LLMSession,
        PersonaResolver,
        Verifier,
    )
    from agent_fleet.orchestration.config import OrchestrationConfig

logger = logging.getLogger(__name__)


def _truncate_verify_message(message: str, *, max_lines: int = 50) -> str:
    """Truncate verify failure output to keep fix-loop prompts small.

    Whole pytest dumps balloon the IMPLEMENT prompt and inflate token cost.
    Keep the first ``max_lines`` lines; mark the elision so the agent knows
    output was clipped.
    """
    if not message:
        return message
    lines = message.splitlines()
    if len(lines) <= max_lines:
        return message
    omitted = len(lines) - max_lines
    return "\n".join(lines[:max_lines]) + f"\n... [{omitted} more lines truncated]"


def _task_spec_with_browser_research(task_spec: TaskSpec) -> TaskSpec:
    if not task_spec.research_plan:
        return task_spec
    updated = [{**item, "needs_browser": True} for item in task_spec.research_plan]
    return replace(task_spec, research_plan=updated)


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
    max_verify_retries: int = 1
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
    # Per-phase token rollup captured inside the runner's bind_run scope.
    # The dispatcher's get_run_log() runs after that scope exits and sees an
    # empty RunLog, so the full pipeline's RESEARCH/PLAN/SYNTHESIZE usage is
    # carried out here instead of being lost. See _run_end_kwargs.
    usage_rollup: dict[str, Any] | None = None


def _run_end_kwargs(result: FleetRunResult, repo: RepoConfig | None) -> dict[str, Any]:
    """Extra run.end payload: outcome_metrics for per-repo level-up analysis."""
    run_log = get_run_log()
    usage_rollup = (
        run_log.usage_rollup_snapshot(task_id=result.task_id) if run_log is not None else None
    )
    # Carry the rollup out on the result; the dispatcher reads it after this
    # bind_run scope has closed, when get_run_log() no longer sees this RunLog.
    result.usage_rollup = usage_rollup
    repo_key_value = level_up_repo_key(
        name=repo.name if repo else None,
        repo_root=repo.repo_root if repo else None,
    )
    outcome_metrics = build_run_metrics(
        status=result.outcome,
        phases=result.phases,
        error=result.error,
        pr_number=result.pr_number,
        review_verdict=review_verdict_from_runner_result(result),
        usage_rollup=usage_rollup,
        changed_files_count=len(result.changed_files),
        duration_seconds=result.duration_seconds,
        repo_key=repo_key_value,
        issue_number=result.task_id,
    )
    payload: dict[str, Any] = {"outcome_metrics": outcome_metrics}
    if result.error:
        payload["error"] = result.error
    if result.pr_number is not None:
        payload["pr_number"] = result.pr_number
    return payload


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
        fleet_config: FleetConfig | None = None,
    ) -> None:
        self._backend = backend
        self._persona_resolver = persona_resolver
        self._git_ops = git_ops
        self._verifier = verifier
        self._spine = spine or SpineConfig.defaults()
        self._config = config or FleetRunConfig()
        self._forge = forge
        self._fleet_config = fleet_config

    def _build_phase_graph(self) -> PhaseGraph:
        return default_phase_graph(
            max_verify_retries=self._config.max_verify_retries,
            design_review_enabled=self._spine.design_review_enabled,
            design_visual_surface_globs=tuple(self._spine.design_visual_surface_globs),
        )

    def _resolve_orchestration(self, repo: RepoConfig | None) -> OrchestrationConfig:
        from agent_fleet.orchestration.config import resolve_orchestration_config

        if repo is not None and repo.orchestration is not None:
            return repo.orchestration
        return resolve_orchestration_config(None)

    def _dispatch_decomposed_children(
        self,
        *,
        task_spec: TaskSpec,
        task_id: int,
        title: str,
        body: str,
        persona: str,
        repo_root: Path,
        repo: RepoConfig | None,
        run_id: str,
        phases: dict[str, Any],
        start: float,
        orchestration: OrchestrationConfig,
    ) -> FleetRunResult:
        from agent_fleet.config import load_fleet_config
        from agent_fleet.dispatcher import FleetDispatcher
        from agent_fleet.orchestration.decompose import dispatch_task_spec_children
        from agent_fleet.repo import merge_repo_into_fleet_config

        parent_task = FleetTask(
            goal=title,
            context=body,
            persona=persona,
            workspace=str(repo_root),
            pipeline=orchestration.default_child_pipeline,
            title=title,
        )
        fleet_config = merge_repo_into_fleet_config(
            self._fleet_config or load_fleet_config(),
            repo,
        )
        dispatcher = FleetDispatcher(config=fleet_config)
        child_results, status, error, summary = dispatch_task_spec_children(
            task_spec=task_spec,
            parent_task=parent_task,
            dispatcher=dispatcher,
            child_pipeline=orchestration.default_child_pipeline,
            persona_resolver=self._persona_resolver,
            fallback_persona=persona,
        )
        phases["DECOMPOSE_DISPATCH"] = {
            "child_count": len(child_results),
            "child_pipeline": orchestration.default_child_pipeline,
            "children": [r.__dict__ for r in child_results],
        }
        changed_files: list[str] = []
        for child in child_results:
            if child.changed_files:
                changed_files.extend(child.changed_files)
        outcome = status if status != "completed" else "completed"
        return FleetRunResult(
            run_id=run_id,
            task_id=task_id,
            persona=persona,
            outcome=outcome,
            task_spec=task_spec.to_dict(),
            summary=summary,
            changed_files=sorted(set(changed_files)),
            phases=phases,
            error=error,
            duration_seconds=round(time.monotonic() - start, 2),
        )

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
        issue_labels: list[str] | None = None,
        experience_source: str = "full_pipeline",
        pr_loop_round: int | None = None,
        task_complexity: str | None = None,
        allowed_paths: tuple[str, ...] = (),
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
        dispatch_equip = None
        session: LLMSession | None = None
        browser_session_factory: Callable[[], LLMSession | None] | None = None
        require_mcp = is_visual_audit_dispatch(
            issue_labels=issue_labels,
            title=title,
            body=body,
        )
        # Derive runtime parameters from complexity.  Falls back to the
        # FleetRunConfig value when no complexity is declared.
        _runtime = derive_runtime(task_complexity)
        effective_max_retries = (
            _runtime.retries if task_complexity is not None else self._config.max_verify_retries
        )

        run_log = RunLog.create(
            run_id=run_id,
            issue_number=issue_number or task_id,
            task_id=task_id,
            persona=persona,
            visual_audit=require_mcp,
        )
        phase_graph = self._build_phase_graph()

        with bind_run(run_log, run_log.context):
            run_log.run_start(
                title=title,
                visual_audit=require_mcp,
                phase_order=[p.name for p in phase_graph],
            )
            run_log.emit(
                "phase_graph.order",
                data={"phases": [p.name for p in phase_graph]},
            )
            if require_mcp:
                logger.info("[%s] Playwright MCP required for this task", run_id)
                run_log.emit("mcp.required", data={"servers": ["playwright"]})

            try:
                session = create_fleet_session(
                    self._backend,
                    fleet_config=self._fleet_config,
                    persona_resolver=self._persona_resolver,
                    persona=persona,
                    cwd=repo_root,
                )
                if session is not None and self._fleet_config:
                    persona_spec = self._persona_resolver.load(persona)
                    mcp_specs = {
                        name: self._fleet_config.mcp_servers[name]
                        for name in (getattr(persona_spec, "mcp_servers", []) or [])
                        if name in self._fleet_config.mcp_servers
                    }
                    if mcp_specs:
                        from agent_fleet.hooks import SessionCapableBackend

                        backend = self._backend
                        if isinstance(backend, SessionCapableBackend):

                            def browser_session_factory() -> LLMSession | None:
                                return create_fleet_session(
                                    backend,
                                    fleet_config=self._fleet_config,
                                    persona_resolver=self._persona_resolver,
                                    persona=persona,
                                    cwd=repo_root,
                                )

                if self._config.resume and isinstance(self._git_ops, ResumableGitOps):
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
                        run_log.emit("run.resume", data=phases["RESUME"])

                with run_log.phase("PLAN"):
                    logger.info("[%s] PLAN", run_id)
                    task_spec = plan(
                        task_id,
                        title,
                        body,
                        backend=self._backend,
                        persona_resolver=self._persona_resolver,
                        spine_config=self._spine,
                        session=session,
                    )
                if require_mcp and task_spec.research_plan:
                    task_spec = _task_spec_with_browser_research(task_spec)
                task_spec, decompose_fallback = coerce_empty_decompose(task_spec)
                phases["PLAN"] = task_spec.to_dict()

                repo_cfg = find_repo_config(repo_root)
                fleet_cfg = self._fleet_config or load_fleet_config()
                equip_task = FleetTask(
                    goal=title,
                    context=body,
                    persona=persona,
                    workspace=str(repo_root),
                )
                dispatch_equip = resolve_dispatch_equip(
                    equip_task,
                    fleet_cfg,
                    repo_cfg,
                    run_id=run_id,
                    loadout_size=_runtime.loadout_size,
                )
                phases["EQUIP"] = {
                    "base_loadout": dispatch_equip.base_loadout,
                    "skill_slots_execute": list(dispatch_equip.skill_slots_execute),
                    "skill_slots_review": list(dispatch_equip.skill_slots_review),
                    "compose_chars": len(dispatch_equip.compose_body),
                }
                run_log.emit("equip.resolved", data=phases["EQUIP"])

                if decompose_fallback:
                    phases["DECOMPOSE_FALLBACK"] = {
                        "reason": "empty child_issues_proposed",
                    }
                    run_log.emit(
                        "orchestration.decompose_fallback", data=phases["DECOMPOSE_FALLBACK"]
                    )

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
                    run_log.run_end(
                        outcome=result.outcome,
                        changed_lines=0,
                        **_run_end_kwargs(result, find_repo_config(repo_root)),
                    )
                    return result

                if task_spec.decomposition_decision == DecompositionDecision.DECOMPOSE:
                    repo = find_repo_config(repo_root)
                    orchestration = self._resolve_orchestration(repo)
                    if orchestration.enabled and orchestration.auto_dispatch_children:
                        if session is not None:
                            with contextlib.suppress(Exception):
                                session.dispose()
                            session = None
                        result = self._dispatch_decomposed_children(
                            task_spec=task_spec,
                            task_id=task_id,
                            title=title,
                            body=body,
                            persona=persona,
                            repo_root=repo_root,
                            repo=repo,
                            run_id=run_id,
                            phases=phases,
                            start=start,
                            orchestration=orchestration,
                        )
                        run_log.run_end(
                            outcome=result.outcome,
                            changed_lines=0,
                            **_run_end_kwargs(result, repo),
                        )
                        return result
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
                    run_log.run_end(
                        outcome=result.outcome,
                        changed_lines=0,
                        **_run_end_kwargs(result, find_repo_config(repo_root)),
                    )
                    return result

                if not resume_mode:
                    with run_log.phase("RESEARCH", items=len(task_spec.research_plan)):
                        logger.info(
                            "[%s] RESEARCH (%d items)",
                            run_id,
                            len(task_spec.research_plan),
                        )
                        notes = research_all(
                            task_spec.research_plan,
                            backend=self._backend,
                            memory_limit=self._config.memory_limit_research,
                            max_workers=self._config.max_research_workers,
                            cwd=repo_root,
                            browser_session_factory=browser_session_factory,
                        )
                    phases["RESEARCH"] = [n.to_dict() for n in notes]

                    with run_log.phase("SYNTHESIZE"):
                        logger.info("[%s] SYNTHESIZE", run_id)
                        brief = synthesize(task_spec, notes, backend=self._backend, session=session)
                    phases["SYNTHESIZE"] = brief.to_dict()

                    with run_log.phase("IMPLEMENT"):
                        logger.info("[%s] IMPLEMENT", run_id)
                        worktree = self._git_ops.setup_workspace(
                            repo_root,
                            run_id,
                            base_branch,
                            branch_name=branch_name if self._config.create_branch else None,
                        )
                        if self._config.create_branch and not getattr(
                            self._git_ops, "use_worktree", False
                        ):
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
                            session=session,
                            require_mcp_tools=require_mcp,
                            compose_body=dispatch_equip.compose_body,
                        )
                    phases["IMPLEMENT"] = {"branch": branch_name, "worktree": str(worktree)}

                # --- allowed_paths enforcement ---
                if allowed_paths:
                    assert worktree is not None
                    _all_changed = self._git_ops.changed_files(worktree)
                    _out_of_scope = [
                        p
                        for p in _all_changed
                        if not path_under_allowlist(p, allowed_paths, worktree=worktree)
                    ]
                    if _out_of_scope:
                        _n = len(_out_of_scope)
                        run_log.emit(
                            "scope.violation",
                            data={
                                "allowed": list(allowed_paths),
                                "offending": [str(p) for p in _out_of_scope],
                                "count": _n,
                            },
                        )
                        _first3 = [str(p) for p in _out_of_scope[:3]]
                        result = FleetRunResult(
                            run_id=run_id,
                            task_id=task_id,
                            persona=persona,
                            outcome="scope_violation",
                            task_spec=task_spec.to_dict() if task_spec else None,
                            changed_files=[str(p) for p in _all_changed],
                            phases=phases,
                            error=(f"Agent modified {_n} file(s) outside allowed_paths: {_first3}"),
                            duration_seconds=round(time.monotonic() - start, 2),
                        )
                        run_log.run_end(
                            outcome=result.outcome,
                            changed_lines=_changed_lines(worktree),
                            **_run_end_kwargs(result, find_repo_config(repo_root)),
                        )
                        return result

                assert worktree is not None
                verify_attempts = 0
                verify_result = None
                while verify_attempts <= effective_max_retries:
                    with run_log.phase("VERIFY", attempt=verify_attempts + 1):
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
                    if verify_attempts > effective_max_retries:
                        break
                    # LOW-complexity retry gate: only retry when stderr is
                    # non-empty AND mentions a file the agent wrote.
                    if task_complexity == "LOW":
                        written = tuple(str(f) for f in changed)
                        if not is_actionable_stderr(verify_result.message, written):
                            logger.info(
                                "[%s] LOW complexity: skipping fix retry (stderr not actionable)",
                                run_id,
                            )
                            run_log.emit(
                                "complexity.low_retry_suppressed",
                                data={"reason": "stderr not actionable"},
                            )
                            break
                    with run_log.phase("FIX", attempt=verify_attempts):
                        if notes is None:
                            logger.info("[%s] RESEARCH (resume retry)", run_id)
                            notes = research_all(
                                task_spec.research_plan,
                                backend=self._backend,
                                memory_limit=self._config.memory_limit_research,
                                max_workers=self._config.max_research_workers,
                                cwd=repo_root,
                                browser_session_factory=browser_session_factory,
                            )
                            phases.setdefault("RESEARCH", [n.to_dict() for n in notes])
                        # Recycle the persistent session before each fix iteration.
                        # The full pipeline reuses one session across PLAN→…→IMPLEMENT,
                        # so its conversation history balloons and re-running
                        # SYNTHESIZE+IMPLEMENT on retry costs ~M tokens of cache reads.
                        # A fresh session per fix keeps the prompt scope minimal.
                        if session is not None:
                            with contextlib.suppress(Exception):
                                session.dispose()
                        session = create_fleet_session(
                            self._backend,
                            fleet_config=self._fleet_config,
                            persona_resolver=self._persona_resolver,
                            persona=persona,
                            cwd=repo_root,
                        )
                        verify_msg = _truncate_verify_message(verify_result.message)
                        brief = synthesize(
                            task_spec,
                            notes,
                            backend=self._backend,
                            extra_context=(f"Verification failed: {verify_msg}. Fix and retry."),
                            session=session,
                        )
                        implement(
                            brief,
                            task_spec,
                            worktree,
                            branch_name,
                            backend=self._backend,
                            persona_resolver=self._persona_resolver,
                            persona_name=persona,
                            prompt_suffix=f"Previous verify failure: {verify_msg}",
                            session=session,
                            require_mcp_tools=require_mcp,
                            compose_body=dispatch_equip.compose_body,
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
                    run_log.run_end(
                        outcome=result.outcome,
                        changed_lines=_changed_lines(worktree),
                        **_run_end_kwargs(result, find_repo_config(repo_root)),
                    )
                    return result

                changed_files = get_changed_files(worktree)
                diff = self._git_ops.diff_summary(worktree)

                phase_ctx = PhaseRunContext(
                    task_spec=task_spec,
                    changed_files=changed_files,
                )
                if should_run_phase(phase_graph, "DESIGN_REVIEW", phase_ctx):
                    with run_log.phase("DESIGN_REVIEW"):
                        run_log.emit(
                            "design_review.skipped",
                            data={"reason": "handler not configured"},
                        )
                    phases["DESIGN_REVIEW"] = {
                        "skipped": True,
                        "reason": "handler not configured",
                    }

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
                    run_log.run_end(
                        outcome=result.outcome,
                        changed_lines=_changed_lines(worktree),
                        **_run_end_kwargs(result, find_repo_config(repo_root)),
                    )
                    return result

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
                                browser_session_factory=browser_session_factory,
                            )
                        brief = synthesize(task_spec, notes, backend=self._backend, session=session)
                    with run_log.phase("OPEN_PR"):
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

                with run_log.phase("REVIEW"):
                    logger.info("[%s] REVIEW", run_id)
                    review_results = review(
                        pr_number or task_id,
                        diff,
                        changed_files,
                        backend=self._backend,
                        session=session,
                    )
                phases["REVIEW"] = [r.to_dict() for r in review_results]

                phase_ctx = PhaseRunContext(
                    task_spec=task_spec,
                    reviews=review_results,
                    changed_files=changed_files,
                )
                tech_lead: TechLeadReview | None = None
                if should_run_phase(phase_graph, "TECH_LEAD", phase_ctx):
                    with run_log.phase("TECH_LEAD"):
                        logger.info("[%s] TECH_LEAD", run_id)
                        tech_lead = tech_lead_review(
                            task_spec,
                            review_results,
                            pr_number or task_id,
                            backend=self._backend,
                            session=session,
                        )
                    if tech_lead:
                        phases["TECH_LEAD"] = tech_lead.to_dict()

                summary_parts = [brief.summary if brief else ""]
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
                run_log.run_end(
                    outcome=result.outcome,
                    changed_lines=_changed_lines(worktree),
                    pr_number=pr_number,
                    jsonl=str(run_log.jsonl_path) if run_log.jsonl_path else None,
                    **_run_end_kwargs(result, find_repo_config(repo_root)),
                )
                return result
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
                run_log.run_end(
                    outcome="error",
                    changed_lines=_changed_lines(worktree),
                    **_run_end_kwargs(result, find_repo_config(repo_root)),
                )
                return result
            finally:
                if result is not None:
                    record_runner_experience(
                        result=result,
                        title=title,
                        persona=persona,
                        repo_root=repo_root,
                        experience_source=experience_source,
                        pr_loop_round=pr_loop_round,
                        dispatch_equip=dispatch_equip,
                    )
                if session is not None:
                    with contextlib.suppress(Exception):
                        session.dispose()
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

        session = create_fleet_session(
            self._backend,
            fleet_config=self._fleet_config,
            persona_resolver=self._persona_resolver,
            persona=self._task.persona,
            cwd=self._workspace,
        )

        try:
            phase_results, summary, exit_code, changed_files = run_pipeline(
                backend=self._backend,
                resolver=self._persona_resolver,
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
    fleet_config: FleetConfig | None = None,
    task_complexity: str | None = None,
    allowed_paths: tuple[str, ...] = (),
) -> FleetRunResult:
    """Convenience entry: discover repo config and run full pipeline."""
    from agent_fleet.config import load_fleet_config
    from agent_fleet.integrations.command_verifier import CommandVerifier
    from agent_fleet.integrations.local_git import git_ops_from_repo

    ws = Path(workspace or Path.cwd()).resolve()
    repo = find_repo_config(ws) or RepoConfig(repo_root=ws)
    if workspace:
        repo.repo_root = ws
    resolved_config = fleet_config or load_fleet_config()
    runner = LocalFleetRunner(
        backend=backend,
        persona_resolver=persona_resolver,
        git_ops=git_ops_from_repo(repo),
        verifier=CommandVerifier(repo),
        spine=_spine_from_repo(repo),
        fleet_config=resolved_config,
    )
    body = goal if not context else f"{goal}\n\n## Context\n{context}"
    return runner.run(
        task_id=task_id or int(time.time()) % 100000,
        title=title or goal[:120],
        body=body,
        persona=persona or repo.default_persona,
        repo_root=ws,
        base_branch=repo.default_branch,
        experience_source="cli",
        task_complexity=task_complexity,
        allowed_paths=allowed_paths,
    )
