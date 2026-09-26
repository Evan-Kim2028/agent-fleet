"""Orchestrate one lane: worktree -> engine -> PR guarantee -> gate -> status.

This is the module the CLI calls. The order is the whole point:

1. **Worktree** — create or reuse, so no prior work is abandoned.
2. **Register** — record the lane's process identity *before* spawning, so a
   concurrent ``lanes stop`` has something valid to signal.
3. **Engine** — run the implementer under a memory cap, with the standing
   fences in its prompt. The run log goes *outside* the worktree, because the
   guarantee below stages with ``git add -A``. A run that ends mid-intention
   without touching the tree is nudged once, and re-judged.
4. **Guarantee the PR** — runs even when the engine failed, because the recurring
   failure this replaces was exactly "the agent left work, nobody opened the PR".
   An engine that died with real work on the branch is the case that most needs
   the guarantee. When there is nothing to publish, the implementer's own final
   message is the reason — see ``no_changes_stopped`` / ``lazy_exit``.
5. **Binding** — verify the repo came from the worktree's own ``origin`` and the
   PR's ``headRefName`` is this lane's branch. **Refusing here is the point**:
   a stray ``REVIEW_REPO`` once sent four review lenses at another repo's PR.
6. **Gate** — feature-detected; skipped cleanly when absent.
7. **Status line** — append ``HH:MM:SS PREMERGE-APPROVED <sha9>``,
   ``HH:MM:SS NEEDS-ESCALATION <reason>``, or
   ``HH:MM:SS GATE-SKIPPED PR #<n> @<sha9> (<reason>)``, the contract
   ``automerge.sh`` and both operators' tooling read.
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
    has_publishable_work,
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
from agent_fleet.fleet_ops.statusfile import gate_skipped_line, hook_env, run_hook
from agent_fleet.fleet_ops.worktree import default_run_dir, ensure_lane_worktree

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

#: How much of the implementer's final message is kept in an escalation detail
#: and in the status line. Long enough to carry the reason, short enough that a
#: model that pasted its whole transcript cannot push the actionable sentence
#: out of the truncated status line.
FINAL_TEXT_DETAIL_CHARS = 1500

#: The status line's own field cap. A status line is read by a human scanning a
#: file and by the automerge's grep; an unbounded reason made both worse.
STATUS_FIELD_CHARS = 200

#: How much of the implementer's final message rides along in the status line,
#: after the reason token. The line is capped at :data:`STATUS_FIELD_CHARS` in
#: total, so this is what is left for the explanation.
STATUS_REASON_DETAIL_CHARS = 120

#: Reasons that mean "the implementer chose to stop", as opposed to "it ran out
#: of steam". Both are legitimate lane outcomes; only the second is worth an
#: automatic retry, and only the first belongs on a human's decision list.
REASON_NO_CHANGES_STOPPED = "no_changes_stopped"
REASON_LAZY_EXIT = "lazy_exit"


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
    #: Hook ids that refused the guarantee's commit, and the reason the engine
    #: produced no publishable work. Both exist so the operator reading only the
    #: result (or only the status file) sees *which* hook and *why*.
    hooks_failed: list[str] = field(default_factory=list)
    no_change_detail: str = ""

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
            "hooks_failed": self.hooks_failed,
            "no_change_detail": self.no_change_detail,
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
        HH:MM:SS GATE-SKIPPED PR #<n> @<sha9> (<reason>)

    Appended, never truncated: automerge tails the file, so history matters.
    A sha that is not a real short sha is *not* emitted — the bash automerge took
    the last field of the line and would have tried to merge a PR whose head
    started with that string, found none, and looped forever.
    """
    short_sha = sha9.strip()[:9] if sha9 else ""
    if approved and _SHA_RE.match(short_sha):
        line = f"{local_hhmmss()} PREMERGE-APPROVED {short_sha}"
    else:
        clean = " ".join((reason or "unknown").split())[:STATUS_FIELD_CHARS]
        line = f"{local_hhmmss()} NEEDS-ESCALATION {clean}"

    return _append_status(status_file, line, event=event)


def write_gate_skipped_line(
    status_file: Path | str | None,
    *,
    pr: int | None,
    sha: str | None,
    reason: str,
    event: Callable[[str], None] | None = None,
) -> str:
    """Append the ``GATE-SKIPPED`` line. See
    :func:`agent_fleet.fleet_ops.statusfile.gate_skipped_line`."""
    return _append_status(status_file, gate_skipped_line(pr, sha=sha, reason=reason), event=event)


def _append_status(
    status_file: Path | str | None, line: str, *, event: Callable[[str], None] | None
) -> str:
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


def _no_change_detail(result: LaneRunResult, final_text: str) -> str:
    """Record the implementer's own account on the result, and return it.

    Set here rather than at the classification site because it must happen on
    exactly the paths that *use* it: a later, unrelated escalation that inherited
    a stale ``no_change_detail`` would put a decision that was never made into
    the next lane's status line.

    The *tail* of the message is kept, because a model that reached a decision
    explains itself last; the first few hundred characters are a recap of work
    that was never done.
    """
    result.no_change_detail = final_text[-FINAL_TEXT_DETAIL_CHARS:]
    return result.no_change_detail


def _status_reason(reason: str, result: LaneRunResult) -> str:
    """The one-line reason on the status file, carrying the actionable facts.

    A bare token is not enough for either of the two cases that get read by
    someone who was not watching: ``commit_failed`` does not say *which* hook
    refused, and ``no_changes_stopped`` does not say what the implementer
    decided. Both facts already exist on the result, so the line carries them
    rather than sending the operator to the transcript.
    """
    parts = [reason]
    if result.hooks_failed:
        parts.append(f"hooks_failed=[{', '.join(result.hooks_failed)}]")
    if result.no_change_detail:
        # The tail of the final message is the decision; the first words are a
        # recap of work the operator does not need in a status line.
        text = " ".join(result.no_change_detail.split())
        parts.append(text[-STATUS_REASON_DETAIL_CHARS:])
    return " ".join(parts)


def _run_id() -> str:
    """A sortable, filesystem-safe id for one run: ``YYYYmmdd-HHMMSS-<pid>``."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{os.getpid()}"


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
    gate: bool = True,
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
            reason=_status_reason(reason, result),
            event=lambda line: _event("status", line=line),
        )
        _event("lane.escalated", reason=reason, detail=detail[:500])
        # The lane is over the moment its verdict is recorded, so the process
        # identity it wrote before the engine spawn goes with it. The two
        # no-change escalations below return before the mid-run teardown, and a
        # record left naming a live-looking process group is what a later
        # `lanes stop` signals: on a host running many agents that group can
        # belong to an unrelated tree.
        update_record(
            result.operator,
            result.lane,
            state=STATE_ESCALATED,
            phase=PHASE_DONE,
            reason=reason,
            last_event="lane.escalated",
            status_line=result.status_line,
            pid=None,
            pgid=None,
            starttime=None,
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
    # Outside the worktree by default: the guarantee stages with `git add -A`,
    # and a run log that lands in the branch is both PR pollution and, for a
    # lane that changed nothing, a commit of only a log that then fails hooks.
    run_path = Path(run_dir) if run_dir else default_run_dir(operator, lane, run_id=_run_id())

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

    def invoke_engine(engine_prompt: str, *, name: str) -> engines.EngineResult:
        """One engine invocation. A raised exception becomes a failed result.

        Wrapped rather than inlined because the lazy-exit retry below runs the
        same call a second time, and a retry that failed to be caught would
        leave the lane with no verdict at all.
        """
        try:
            if selected_engine == "devin":
                return engines.run_devin_engine(
                    workdir=workdir,
                    prompt=engine_prompt,
                    run_dir=run_path,
                    name=name,
                    pr_exists=pr_probe,
                    runner=runner,
                )
            if selected_engine == "cmd":
                return engines.run_cmd_engine(
                    workdir=workdir,
                    prompt=engine_prompt,
                    run_dir=run_path,
                    name=name,
                    pr_exists=pr_probe,
                    runner=runner,
                )
            raise ValueError(f"unsupported engine {selected_engine!r}")
        except Exception as exc:
            _event("lane.engine.error", detail=str(exc)[:500])
            return engines.EngineResult(
                engine=selected_engine,
                model="",
                exit_code=1,
                detail=f"engine invocation failed: {exc}",
            )

    def record_engine_done(res: engines.EngineResult) -> str:
        """Log the engine's verdict and return the stream text it produced."""
        result.engine_result = res
        text = ""
        if res.stream_path is not None:
            try:
                text = res.stream_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
        _event(
            "lane.engine.done",
            exit_code=res.exit_code,
            tool_calls=res.tool_calls,
            tool_errors=lazyexit.count_tool_errors(text),
            lazy=res.lazy,
            resumes=res.resumes,
        )
        return text

    engine_result = invoke_engine(prompt, name="impl")
    stream_text = record_engine_done(engine_result)

    # A run that stopped mid-intention without touching the tree gets exactly one
    # more attempt, here rather than by escalating: the difference between "ran
    # out of steam" and "decided to stop" is not knowable from a single run, and
    # a nudge costs one engine run while a wrong escalation costs an operator.
    if not has_publishable_work(
        workdir, push_branch, base, runner=runner
    ) and lazyexit.looks_like_unfinished_intention(engine_result.final_text):
        _event("lane.engine.retry", reason=REASON_LAZY_EXIT, attempt=1)
        logger.info("lane %s stopped mid-intention with no changes; one nudge", lane)
        retry_result = invoke_engine(engines.NUDGE_PROMPT, name="impl-nudge")
        stream_text = f"{stream_text}\n{record_engine_done(retry_result)}"
        engine_result = retry_result
        if not has_publishable_work(workdir, push_branch, base, runner=runner):
            # Still nothing after the nudge, and still talking about work it was
            # about to do: a genuine lazy exit, not a decision.
            detail = _no_change_detail(result, engine_result.final_text)
            return _escalate(
                result,
                reason=REASON_LAZY_EXIT,
                detail=(
                    f"{detail}\n"
                    "the implementer produced no changes and ended mid-intention; "
                    "one automatic retry did not change that"
                ).strip(),
                exit_code=engine_result.exit_code,
            )
    elif not has_publishable_work(workdir, push_branch, base, runner=runner):
        # Nothing to publish, but the implementer said why. That reason is the
        # lane's outcome — a fence or an owner decision belongs on a human's
        # list, not in a generic "no commits ahead" the operator has to
        # reconstruct by reading the transcript. A stream with no final text has
        # no account to give, and inventing one would be worse than saying so:
        # the guarantee's own `no_commits_ahead` is the honest verdict then.
        final_text = engine_result.final_text
        has_own_account = bool(final_text.strip()) and final_text.strip() != "NO RESULT EVENT"
        if has_own_account and not pr_probe():
            return _escalate(
                result,
                reason=REASON_NO_CHANGES_STOPPED,
                detail=_no_change_detail(result, final_text),
                exit_code=engine_result.exit_code,
            )

    tool_errors = lazyexit.count_tool_errors(stream_text)
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
        no_changes_detail=result.no_change_detail,
    )
    result.hooks_failed = list(guarantee.hooks_failed)
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

    # --- 6. gate (feature-detected; off when an external gate owns review) --
    if gate:
        outcome = gate_mod.run_gate(
            lane=lane,
            binding=bound.binding,
            cwd=workdir,
            judge_engine=spec.judge_engine if spec else None,
            known_subcommands=known_gate_subcommands,
            env={**os.environ, **binding_mod.gate_env(bound.binding)},
            runner=gate_runner,
        )
    else:
        outcome = gate_mod.GateOutcome(
            available=False,
            reason="gate disabled (--no-gate); PR is guaranteed, an external gate reviews it",
        )
    result.gate = outcome

    if outcome.skipped:
        _event("gate.skipped", reason=outcome.reason, pr=guarantee.pr)
        result.state = STATE_PR_GUARANTEED
        result.reason = outcome.reason
        # Not an escalation: the PR is guaranteed and the external gate owns it
        # from here. Writing it as one made a healthy lane read as a broken one.
        result.status_line = write_gate_skipped_line(
            status_file,
            pr=guarantee.pr,
            sha=result.head,
            reason=outcome.reason,
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
