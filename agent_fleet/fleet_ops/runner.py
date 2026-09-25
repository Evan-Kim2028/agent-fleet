"""Orchestrate one lane: worktree -> engine -> PR guarantee -> gate -> status.

This is the module the CLI calls. The order is the whole point:

1. **Worktree** — create or reuse, so no prior work is abandoned.
2. **Register** — record the lane's process identity *before* spawning, so a
   concurrent ``lanes stop`` has something valid to signal.
3. **Engine** — run the implementer under a memory cap, with the standing
   fences in its prompt. A lazy exit is recorded but is *not* fatal: the next
   step still runs.
4. **Guarantee the PR** — runs even when the engine failed, because the recurring
   failure this replaces was exactly "the agent left work, nobody opened the PR".
   An engine that died with real work on the branch is the case that most needs
   the guarantee.
5. **Binding** — verify the repo came from the worktree's own ``origin`` and the
   PR's ``headRefName`` is this lane's branch. **Refusing here is the point**: a
   stray ``REVIEW_REPO`` once sent four review lenses at another repo's PR.
6. **Gate** — feature-detected; skipped cleanly when absent.
7. **Status line** — append ``HH:MM:SS PREMERGE-APPROVED <sha9>`` or
   ``HH:MM:SS NEEDS-ESCALATION <reason>``, the contract ``automerge.sh`` and
   both operators' tooling read.
8. **Hooks** — the operator's ``on_approved`` or ``on_escalated`` command.

The status file is written even on failure. An operator watching a status file
that simply stops updating cannot tell "still working" from "died", and that
ambiguity is how lanes got lost.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_fleet.fleet_ops import binding as binding_mod
from agent_fleet.fleet_ops import engines, lazyexit
from agent_fleet.fleet_ops import gate as gate_mod
from agent_fleet.fleet_ops.config import (
    DEFAULT_BASE_BRANCH,
    DEFAULT_ENGINE,
    FleetOpsConfig,
    OperatorSpec,
    effective_stall_minutes,
    load_fleet_ops_config_from_repo,
)
from agent_fleet.fleet_ops.guarantee import (
    GuaranteeResult,
    ensure_pull_request,
    head_sha,
    resolve_push_target,
)
from agent_fleet.fleet_ops.registry import (
    PHASE_DONE,
    PHASE_GATE,
    PHASE_IMPL,
    STATE_APPROVED,
    STATE_ESCALATED,
    STATE_PR_GUARANTEED,
    STATE_RUNNING,
    append_event,
    local_hhmmss,
    process_starttime,
    update_record,
)
from agent_fleet.fleet_ops.statusfile import hook_env, run_hook
from agent_fleet.fleet_ops.worktree import ensure_lane_worktree

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable

logger = logging.getLogger(__name__)

#: A short sha is 7-9 hex chars. The status line carries the *prefix*, but the
#: value reaching this check may be a full 40-char sha (that is what
#: ``gh pr list --json headRefOid`` returns), so the match is against the
#: truncated form rather than the raw one — a full sha must still produce a
#: PREMERGE-APPROVED line, not silently fall through to an escalation.
_SHA_RE = re.compile(r"^[0-9a-f]{7,9}$")


@dataclass
class LaneRunResult:
    """Everything one ``lane run`` produced."""

    lane: str
    operator: str
    engine: str
    worktree: Path
    branch: str
    pr: int | None = None
    status_line: str = ""
    state: str = STATE_ESCALATED
    reason: str = ""
    detail: str = ""
    head: str | None = None
    repo: str | None = None
    guarantee: GuaranteeResult | None = None
    gate: gate_mod.GateOutcome | None = None
    binding: binding_mod.LaneBinding | None = None
    engine_result: engines.EngineResult | None = None
    hook_output: str = ""
    events: list[str] = field(default_factory=list)

    @property
    def approved(self) -> bool:
        return self.state == STATE_APPROVED

    @property
    def escalated(self) -> bool:
        return self.state == STATE_ESCALATED

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "operator": self.operator,
            "engine": self.engine,
            "repo": self.repo,
            "worktree": str(self.worktree),
            "branch": self.branch,
            "pr": self.pr,
            "status_line": self.status_line,
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
            "head": self.head,
            "guarantee": self.guarantee.to_dict() if self.guarantee else None,
            "gate": self.gate.to_dict() if self.gate else None,
            "binding": self.binding.to_dict() if self.binding else None,
            "engine_result": self.engine_result.to_dict() if self.engine_result else None,
            "hook_output": self.hook_output,
        }


def write_status_line(
    status_file: Path | str | None,
    *,
    approved: bool,
    sha9: str | None = None,
    reason: str = "",
    event: Callable[[str], None] | None = None,
) -> str:
    """Append the operator-facing status line, and return it.

    The format is fixed by the consumers (``automerge.sh`` and the documents-0e
    automerge both grep for these markers)::

        HH:MM:SS PREMERGE-APPROVED <sha9>
        HH:MM:SS NEEDS-ESCALATION <reason>

    Appended, never truncated: automerge tails the file, so history matters.
    A sha that is not a real short sha is *not* emitted — the bash automerge took
    the last field of the line and would have tried to merge a PR whose head
    started with that string, found none, and looped forever.
    """
    short_sha = sha9.strip()[:9] if sha9 else ""
    if approved and _SHA_RE.match(short_sha):
        line = f"{local_hhmmss()} PREMERGE-APPROVED {short_sha}"
    else:
        clean = " ".join((reason or "unknown").split())[:200]
        line = f"{local_hhmmss()} NEEDS-ESCALATION {clean}"

    if status_file is not None:
        path = Path(status_file).expanduser()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            logger.warning("could not write status file %s: %s", path, exc)
    if event is not None:
        event(line)
    return line


def _pr_exists(workdir: Path, branch: str) -> bool:
    """Whether an open PR already exists for *branch* — the engines' stop signal.

    Both engines use this to avoid a pointless continue: if the work is already
    published, there is nothing left to continue.
    """
    from agent_fleet.code_review.publish import find_pr_for_branch

    try:
        return find_pr_for_branch(branch, cwd=workdir) is not None
    except OSError:
        return False


def run_lane(
    *,
    operator: str,
    lane: str,
    repo_path: Path | str,
    task_file: Path | str,
    engine: str | None = None,
    branch: str | None = None,
    status_file: Path | str | None = None,
    config: FleetOpsConfig | None = None,
    known_gate_subcommands: set[str] | None = None,
    run_dir: Path | str | None = None,
    expected_slug: str | None = None,
    worktree_parent: Path | str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    gate_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    hook_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> LaneRunResult:
    """Run one lane end to end. See the module docstring for the ordering."""
    repo = Path(repo_path).expanduser().resolve()
    if config is None:
        config = load_fleet_ops_config_from_repo(repo) or FleetOpsConfig()

    spec: OperatorSpec | None = config.operator(operator)
    selected_engine = (engine or (spec.engine if spec else DEFAULT_ENGINE)).strip().lower()
    base = (
        spec.base_branch if spec and spec.base_branch else config.base_branch
    ) or DEFAULT_BASE_BRANCH
    configured_branch = branch or (spec.branch_for(lane) if spec else None) or f"fb/{lane}"

    events: list[str] = []

    def _event(name: str, **fields: Any) -> None:  # noqa: ANN401
        events.append(name)
        append_event(operator, lane, name, **fields)

    def _fire_hook(
        command: str | None,
        *,
        verdict: str,
        exit_code: int | None,
        sha: str | None,
        pr: int | None,
    ) -> str:
        if not command:
            return ""
        outcome = run_hook(
            command,
            hook_env(
                lane=lane,
                operator=operator,
                repo=str(repo),
                pr=pr,
                sha=sha,
                status_file=str(status_file) if status_file else "",
                verdict=verdict,
                exit_code=exit_code,
            ),
            cwd=repo,
            runner=hook_runner,
        )
        _event(
            f"hook.{verdict.lower()}",
            ok=outcome.ok,
            exit_code=outcome.exit_code,
            detail=outcome.detail[:300],
        )
        return outcome.detail or ("" if outcome.ok else f"hook exited {outcome.exit_code}")

    def _escalate(
        result: LaneRunResult,
        *,
        reason: str,
        detail: str = "",
        exit_code: int | None = None,
    ) -> LaneRunResult:
        result.state = STATE_ESCALATED
        result.reason = reason
        result.detail = detail
        result.status_line = write_status_line(
            status_file,
            approved=False,
            reason=reason,
            event=lambda line: _event("status", line=line),
        )
        _event("lane.escalated", reason=reason, detail=detail[:500])
        update_record(
            result.operator,
            result.lane,
            state=STATE_ESCALATED,
            phase=PHASE_DONE,
            reason=reason,
            last_event="lane.escalated",
            status_line=result.status_line,
        )
        # documents-1d's monitor keys off `exit=` lines in this hook, so a stall
        # or a lazy exit has to reach it exactly like a gate rejection does.
        result.hook_output = _fire_hook(
            spec.on_escalated if spec else None,
            verdict="escalate",
            exit_code=exit_code,
            sha=result.head,
            pr=result.pr,
        )
        result.events = events
        return result

    _event("lane.started", engine=selected_engine, branch=configured_branch, repo=str(repo))

    # --- 1. worktree -------------------------------------------------------
    try:
        wt = ensure_lane_worktree(
            repo,
            lane=lane,
            branch=configured_branch,
            base=base,
            parent=Path(worktree_parent) if worktree_parent is not None else None,
            runner=runner,
        )
    except (FileNotFoundError, FileExistsError, RuntimeError) as exc:
        return _escalate(
            LaneRunResult(
                lane=lane,
                operator=operator,
                engine=selected_engine,
                worktree=repo,
                branch=configured_branch,
            ),
            reason="worktree_failed",
            detail=str(exc),
        )

    workdir = wt.path
    # An existing PR's head wins, so we never create a competing PR.
    push_branch, why = resolve_push_target(wt.branch, cwd=workdir, runner=runner)
    slug = binding_mod.origin_slug(workdir, runner=runner)
    _event("lane.worktree", path=str(workdir), created=wt.created, push_branch=push_branch, why=why)

    update_record(
        operator,
        lane,
        state=STATE_RUNNING,
        phase=PHASE_IMPL,
        engine=selected_engine,
        worktree=str(workdir),
        branch=push_branch,
        repo_path=str(repo),
        repo=slug,
        head=None,
    )

    result = LaneRunResult(
        lane=lane,
        operator=operator,
        engine=selected_engine,
        worktree=workdir,
        branch=push_branch,
        repo=slug,
    )

    # --- 2. task + prompt --------------------------------------------------
    try:
        task_text = engines.read_task_file(task_file)
    except FileNotFoundError as exc:
        return _escalate(result, reason="task_file_missing", detail=str(exc))

    prompt = engines.build_prompt(
        task_text,
        lane=lane,
        branch=push_branch,
        extra_fences=config.fences,
    )
    run_path = Path(run_dir) if run_dir else workdir / ".agent-fleet" / "runs" / lane

    # --- 3. engine ---------------------------------------------------------
    # The manager's own identity is recorded *before* the spawn so a concurrent
    # `lanes stop` always has a valid (pid, pgid, starttime) triple to signal,
    # including in the window before the engine child exists.
    update_record(
        operator,
        lane,
        pid=os.getpid(),
        pgid=os.getpgid(0),
        starttime=process_starttime(os.getpid()),
    )
    _event(
        "lane.engine.start",
        engine=selected_engine,
        stall_minutes=effective_stall_minutes(config, operator),
    )

    def pr_probe() -> bool:
        return _pr_exists(workdir, push_branch)

    try:
        if selected_engine == "devin":
            engine_result = engines.run_devin_engine(
                workdir=workdir,
                prompt=prompt,
                run_dir=run_path,
                name="impl",
                pr_exists=pr_probe,
                runner=runner,
            )
        elif selected_engine == "cmd":
            engine_result = engines.run_cmd_engine(
                workdir=workdir,
                prompt=prompt,
                run_dir=run_path,
                name="impl",
                pr_exists=pr_probe,
                runner=runner,
            )
        else:
            raise ValueError(f"unsupported engine {selected_engine!r}")
    except Exception as exc:
        engine_result = engines.EngineResult(
            engine=selected_engine,
            model="",
            exit_code=1,
            detail=f"engine invocation failed: {exc}",
        )
        _event("lane.engine.error", detail=str(exc)[:500])
    result.engine_result = engine_result

    stream_text = ""
    if engine_result.stream_path is not None:
        try:
            stream_text = engine_result.stream_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            stream_text = ""
    tool_errors = lazyexit.count_tool_errors(stream_text)
    _event(
        "lane.engine.done",
        exit_code=engine_result.exit_code,
        tool_calls=engine_result.tool_calls,
        tool_errors=tool_errors,
        lazy=engine_result.lazy,
        resumes=engine_result.resumes,
    )
    update_record(
        operator,
        lane,
        tool_calls=engine_result.tool_calls,
        tool_errors=tool_errors,
        head=head_sha(workdir, runner=runner),
        pid=None,
        pgid=None,
        starttime=None,
    )

    # --- 4. GUARANTEE THE PR (runs even when the engine failed) ------------
    guarantee = ensure_pull_request(
        workdir,
        branch=push_branch,
        base=base,
        engine=selected_engine,
        task_file=str(task_file),
        lane=lane,
        skip_hooks=config.baseline_skip_hooks,
        runner=runner,
        skip_env=config.skip_env(),
    )
    result.guarantee = guarantee
    result.pr = guarantee.pr
    result.head = head_sha(workdir, runner=runner, short=9)
    _event(
        "lane.pr.guaranteed",
        pr=guarantee.pr,
        committed=guarantee.committed,
        pushed=guarantee.pushed,
        reason=guarantee.reason,
        skip=guarantee.skip_env.get("SKIP"),
    )
    update_record(operator, lane, pr=guarantee.pr, head=result.head)

    if guarantee.escalated or guarantee.pr is None:
        return _escalate(
            result,
            reason=guarantee.reason or "pr_not_guaranteed",
            detail=guarantee.detail,
            exit_code=engine_result.exit_code,
        )

    # --- 5. binding: which repo, which PR, which branch --------------------
    update_record(operator, lane, state=STATE_RUNNING, phase=PHASE_GATE)
    bound = binding_mod.resolve(
        workdir, branch=push_branch, expected_slug=expected_slug, runner=runner
    )
    if not bound.ok:
        # A refusal here is exactly the bug that sent lake #3544 to silph #3544.
        # The lane already has its PR; stopping is strictly safer than judging
        # the wrong one.
        _event("lane.binding.refused", reason=bound.reason, detail=bound.detail)
        return _escalate(result, reason=bound.reason, detail=bound.detail)
    assert bound.binding is not None
    result.binding = bound.binding

    # --- 6. gate (feature-detected) ---------------------------------------
    outcome = gate_mod.run_gate(
        lane=lane,
        binding=bound.binding,
        cwd=workdir,
        judge_engine=spec.judge_engine if spec else None,
        known_subcommands=known_gate_subcommands,
        env={**os.environ, **binding_mod.gate_env(bound.binding)},
        runner=gate_runner,
    )
    result.gate = outcome

    if outcome.skipped:
        _event("gate.skipped", reason=outcome.reason)
        result.state = STATE_PR_GUARANTEED
        result.reason = outcome.reason
        result.status_line = write_status_line(
            status_file,
            approved=False,
            reason=f"PR #{guarantee.pr} guaranteed; {outcome.reason}",
            event=lambda line: _event("status", line=line),
        )
        update_record(
            operator,
            lane,
            state=STATE_PR_GUARANTEED,
            phase=PHASE_DONE,
            last_event="gate.skipped",
            reason=outcome.reason,
            status_line=result.status_line,
        )
        result.events = events
        return result

    _event("gate.done", approved=outcome.approved, reason=outcome.reason, sha9=outcome.sha9)

    if not outcome.approved:
        return _escalate(
            result,
            reason=outcome.reason or "gate_not_approved",
            detail=outcome.output[-2000:],
            exit_code=outcome.exit_code,
        )

    # --- 7. status line + on-approved hook ---------------------------------
    sha9 = outcome.sha9 or (result.head or "")[:9]
    result.status_line = write_status_line(
        status_file,
        approved=True,
        sha9=sha9,
        event=lambda line: _event("status", line=line),
    )
    result.state = STATE_APPROVED
    result.reason = outcome.reason
    update_record(
        operator,
        lane,
        state=STATE_APPROVED,
        phase=PHASE_DONE,
        last_event="status",
        reason=outcome.reason,
        status_line=result.status_line,
    )

    result.hook_output = _fire_hook(
        spec.on_approved if spec else None,
        verdict="approve",
        exit_code=outcome.exit_code,
        sha=sha9,
        pr=guarantee.pr,
    )
    result.events = events
    return result


def idle_seconds_for(record_started_ts: float, *, now: float | None = None) -> float:
    return max(0.0, (now if now is not None else time.time()) - record_started_ts)
