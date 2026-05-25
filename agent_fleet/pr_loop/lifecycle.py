"""Address review findings and wait for CI + merge."""

from __future__ import annotations

import logging
import subprocess
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_fleet.agent_mode import parse_agent_mode
from agent_fleet.backends import make_backend
from agent_fleet.config import FleetConfig, load_fleet_config
from agent_fleet.observability.fleet_logger import FleetLogger
from agent_fleet.personas import YamlPersonaResolver
from agent_fleet.pr_loop import github_ops
from agent_fleet.pr_loop.review_parse import (
    find_reviewer_comment,
    has_blocking_findings,
    parse_review_risk,
)
from agent_fleet.repo import RepoConfig, merge_repo_into_fleet_config
from agent_fleet.scope import files_outside_allowed_paths
from agent_fleet.state import (
    STATE_FILENAME,
    get_pr_state,
    load_state,
    merge_cooldown_remaining,
    save_state,
    set_pr_state,
    state_path,
)

if TYPE_CHECKING:
    from agent_fleet.pr_loop.config import PrLoopConfig

logger = logging.getLogger(__name__)

_AGENT_FOOTER = "\U0001f916 Agent:"
_PARK_MARKER = "<!-- agent-fleet:pr-loop:parked -->"


@dataclass
class LifecycleResult:
    status: str
    detail: str = ""


def _diff_is_deletion_only(diff_text: str) -> bool:
    if not diff_text:
        return False
    added = removed = 0
    for line in diff_text.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return removed > 0 and added == 0


def persona_from_branch(branch: str, default_persona: str) -> str:
    parts = branch.split("/")
    if len(parts) >= 3 and parts[0] in ("fleet", "agent"):
        return parts[1]
    if len(parts) >= 2 and parts[0] == "fleet" and parts[1].startswith("task-"):
        return default_persona
    return default_persona


def _git_changed_files(worktree: Path, *, exclude: tuple[str, ...] = ()) -> list[str]:
    exclude_set = set(exclude)
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        cwd=worktree,
        check=False,
    )
    return [
        line[3:].strip()
        for line in status.stdout.splitlines()
        if line.strip() and len(line) > 3 and line[3:].strip() not in exclude_set
    ]


def _pr_file_scope_prefixes(pr_files: list[str]) -> tuple[str, ...]:
    prefixes: set[str] = set()
    for path in pr_files:
        prefixes.add(path)
        if "/" in path:
            prefixes.add(path.rsplit("/", 1)[0] + "/")
    return tuple(prefixes)


def _files_outside_pr_scope(pr_files: list[str], changed: list[str]) -> tuple[str, ...]:
    prefixes = _pr_file_scope_prefixes(pr_files)
    if not prefixes:
        return ()
    return files_outside_allowed_paths(prefixes, changed)


def _persona_covering_files(
    files: list[str],
    repo: RepoConfig,
) -> str | None:
    """Return a persona whose allowlist covers all *files*, if any."""
    for name, paths in repo.persona_scope_allowlist.items():
        if paths and not files_outside_allowed_paths(paths, files):
            return name
    return None


def _merge_scope_out_of_scope(
    persona: str,
    changed: list[str],
    repo: RepoConfig,
) -> list[str]:
    """Out-of-scope paths for merge gate; allow files covered by any persona."""
    if not changed:
        return []

    allowed_paths = repo.persona_scope_allowlist.get(persona, ())
    if allowed_paths and not files_outside_allowed_paths(allowed_paths, changed):
        return []

    uncovered = list(changed)
    for paths in repo.persona_scope_allowlist.values():
        if not paths:
            continue
        uncovered = list(files_outside_allowed_paths(paths, uncovered))
        if not uncovered:
            return []
    return uncovered


def _review_fix_persona(
    loop_config: PrLoopConfig,
    branch_persona: str,
    repo: RepoConfig,
    pr_files: list[str],
) -> str:
    if loop_config.fix_persona:
        return loop_config.fix_persona
    covering = _persona_covering_files(pr_files, repo)
    if covering:
        return covering
    return branch_persona or repo.default_persona or "coder"


def _ci_fix_persona(loop_config: PrLoopConfig, branch_persona: str, repo: RepoConfig) -> str:
    return (
        loop_config.ci_fix_persona
        or loop_config.fix_persona
        or branch_persona
        or repo.default_persona
    )


def _protected_paths(changed: list[str], repo: RepoConfig) -> list[str]:
    blocked: list[str] = []
    for path in changed:
        if any(path.startswith(prefix) for prefix in repo.critical_path_prefixes):
            blocked.append(path)
    return blocked


def park_for_human(
    pr_number: int,
    reason: str,
    *,
    repo_root: Path,
    label: str,
) -> None:
    if github_ops.pr_has_label(pr_number, label, cwd=repo_root):
        comments = github_ops.pr_comments(pr_number, cwd=repo_root)
        if any(_PARK_MARKER in str(c.get("body") or "") for c in comments):
            return
    github_ops.add_pr_label(pr_number, label, cwd=repo_root)
    github_ops.post_pr_comment(
        textwrap.dedent(f"""\
            **Auto-merge parked** — human review required.

            {reason}

            Remove the `{label}` label after review to allow the watcher to retry.

            {_PARK_MARKER}
        """),
        pr_number,
        cwd=repo_root,
    )


def _file_scope_violation_followup(
    *,
    pr_number: int,
    branch: str,
    violation_detail: str,
    review_body: str,
    repo: RepoConfig,
) -> int | None:
    """File a follow-up issue for review findings that lie outside this PR's scope.

    Returns the new issue number, or None if filing failed.
    """
    persona = persona_from_branch(branch, default_persona=repo.default_persona or "coder")
    title = f"[follow-up from PR #{pr_number}] Out-of-scope review findings"
    body = textwrap.dedent(
        f"""\
        Spun off automatically by the PR-loop because the reviewer-suggested fix touches files
        outside the original PR scope. The PR's own CI fix path will continue independently.

        **Source PR:** #{pr_number} (branch `{branch}`, persona `{persona}`)

        **Scope violation detail:**
        ```
        {violation_detail}
        ```

        **Original review findings:**

        {review_body or "_(no review body captured)_"}

        ---
        Filed by agent-fleet PR-loop. Triage and dispatch as a normal issue when ready.
        """
    )
    return github_ops.create_issue(
        title=title,
        body=body,
        labels=["agent-fleet", "follow-up", f"persona:{persona}"],
        cwd=repo.repo_root,
    )


def poll_for_review_comment(
    pr_number: int,
    *,
    repo_root: Path,
    marker: str,
    timeout_s: int,
    poll_s: int = 10,
) -> str | None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        comments = github_ops.pr_comments(pr_number, cwd=repo_root)
        body = find_reviewer_comment(comments, marker=marker)
        if body:
            return body
        time.sleep(poll_s)
    return None


def address_review_findings(
    *,
    pr_number: int,
    branch: str,
    review_body: str,
    repo: RepoConfig,
    loop_config: PrLoopConfig,
    fleet_config: FleetConfig,
    worktree: Path,
) -> LifecycleResult:
    if not has_blocking_findings(
        review_body,
        deletion_only=_diff_is_deletion_only(github_ops.pr_diff(pr_number, cwd=repo.repo_root)),
    ):
        return LifecycleResult("no_findings", "Review has no blocking findings")

    pr_files = github_ops.pr_changed_files(pr_number, cwd=repo.repo_root)
    branch_persona = persona_from_branch(branch, repo.default_persona)
    fix_persona_name = _review_fix_persona(loop_config, branch_persona, repo, pr_files)
    config = merge_repo_into_fleet_config(fleet_config, repo)
    resolver = YamlPersonaResolver(config)
    persona_obj = resolver.load(fix_persona_name)
    backend = make_backend(config)

    verify_block = ""
    if repo.verify_commands:
        verify_block = "\n".join(f"- `{cmd}`" for cmd in repo.verify_commands)

    pr_files_block = "\n".join(f"- `{path}`" for path in pr_files) or "- (unknown)"

    prompt = textwrap.dedent(f"""\
        The PR analyzer posted review findings on PR #{pr_number}. Address every
        blocking finding in the review comment below.

        ## Review
        {review_body}

        ## PR changed files (only edit these or subpaths)
        {pr_files_block}

        ## Instructions
        1. Read each finding and fix valid issues in the relevant files above.
        2. Do NOT edit files outside this PR's changed paths.
        3. Run verify commands before finishing:
        {verify_block or "- (none configured)"}
        4. Do NOT commit or push — the orchestrator commits after this phase.
        5. If a finding is a false positive, note it but do not change code for it.
    """)

    logger.info(
        "Review fix PR #%s persona=%s worktree=%s pr_files=%d",
        pr_number,
        fix_persona_name,
        worktree,
        len(pr_files),
    )
    result = backend.run(
        prompt,
        max_tokens=0,
        timeout_s=config.timeout_seconds,
        cwd=worktree,
        model=persona_obj.model,
        mode=parse_agent_mode(persona_obj.mode),
        allowed_tools=list(persona_obj.allowed_tools),
    )
    if result.exit_code != 0:
        detail = result.stderr or "Fix agent failed"
        logger.warning("Review fix failed PR #%s: %s", pr_number, detail[:500])
        return LifecycleResult("fix_failed", detail)

    changed = _git_changed_files(worktree, exclude=(STATE_FILENAME,))
    violating = _files_outside_pr_scope(pr_files, changed)
    if violating:
        logger.warning(
            "Review fix scope violation PR #%s: %s",
            pr_number,
            violating,
        )
        return LifecycleResult("scope_violation", f"Out of PR scope: {violating}")

    message = (
        f"fix(fleet): address PR review feedback\n\n"
        f"{_AGENT_FOOTER} persona={fix_persona_name} | PR #{pr_number}"
    )
    pushed = github_ops.commit_and_push(
        worktree, message, branch, exclude=(STATE_FILENAME,)
    )
    if not pushed:
        return LifecycleResult("ignored", "Review had findings but no fix commit was pushed")
    return LifecycleResult("addressed", "Fix pushed for review findings")


def wait_for_ci_green(
    pr_number: int,
    *,
    repo_root: Path,
    loop_config: PrLoopConfig,
    timeout_s: int | None = None,
) -> LifecycleResult:
    deadline = time.time() + (timeout_s or loop_config.ci_poll_timeout_s)
    while time.time() < deadline:
        all_checks, pending, failed = github_ops.pr_checks(
            pr_number,
            cwd=repo_root,
            ignored=loop_config.ignored_ci_checks,
        )
        if not all_checks:
            time.sleep(loop_config.ci_register_poll_s)
            continue
        if failed:
            names = [str(c.get("name", "")) for c in failed]
            return LifecycleResult("ci_failed", f"Failed checks: {names}")
        if not pending:
            return LifecycleResult("ci_green", "All checks passed")
        time.sleep(loop_config.ci_poll_s)
    return LifecycleResult("ci_timeout", "CI did not pass within timeout")


def attempt_ci_fix(
    *,
    pr_number: int,
    branch: str,
    failed_checks: list[str],
    repo: RepoConfig,
    loop_config: PrLoopConfig,
    fleet_config: FleetConfig,
    worktree: Path,
    persona: str,
) -> bool:
    fix_persona_name = _ci_fix_persona(loop_config, persona, repo)
    config = merge_repo_into_fleet_config(fleet_config, repo)
    resolver = YamlPersonaResolver(config)
    persona_obj = resolver.load(fix_persona_name)
    backend = make_backend(config)

    verify_block = "\n".join(f"- `{cmd}`" for cmd in repo.verify_commands) or "- (none)"
    prompt = textwrap.dedent(f"""\
        CI failed on PR #{pr_number}. Fix the failures caused by this branch.

        Failed checks: {", ".join(failed_checks)}

        Verify commands:
        {verify_block}

        Do NOT commit or push — the orchestrator commits after this phase.
        Do NOT weaken CI workflows to make checks pass.
    """)
    logger.info(
        "CI fix PR #%s persona=%s worktree=%s checks=%s",
        pr_number,
        fix_persona_name,
        worktree,
        failed_checks,
    )
    result = backend.run(
        prompt,
        max_tokens=0,
        timeout_s=config.timeout_seconds,
        cwd=worktree,
        model=persona_obj.model,
        mode=parse_agent_mode(persona_obj.mode),
        allowed_tools=list(persona_obj.allowed_tools),
    )
    if result.exit_code != 0:
        return False
    message = (
        f"fix(fleet): CI failures on PR #{pr_number}\n\n{_AGENT_FOOTER} persona={fix_persona_name}"
    )
    return github_ops.commit_and_push(worktree, message, branch, exclude=(STATE_FILENAME,))


def tiered_merge_allowed(
    *,
    ci_green: bool,
    risk: str | None,
    out_of_scope: list[str],
    parked: bool,
) -> tuple[bool, str]:
    reasons: list[str] = []
    if not ci_green:
        reasons.append("CI not green")
    if risk and risk.upper() in {"MEDIUM", "HIGH", "CRITICAL"}:
        reasons.append(f"review risk {risk.upper()}")
    if out_of_scope:
        reasons.append("out-of-scope files: " + ", ".join(out_of_scope))
    if parked:
        reasons.append("needs-human-review label present")
    if reasons:
        return False, "; ".join(reasons)
    return True, ""


def try_merge(
    *,
    pr_number: int,
    persona: str,
    repo: RepoConfig,
    loop_config: PrLoopConfig,
) -> LifecycleResult:
    repo_root = repo.repo_root
    state = load_state(state_path(repo_root))
    remaining = merge_cooldown_remaining(state, loop_config.merge_cooldown_s)
    if remaining > 0:
        return LifecycleResult(
            "cooldown",
            f"Merge cooldown ({remaining:.0f}s remaining)",
        )
    if github_ops.pr_has_blocking_review(pr_number, cwd=repo_root):
        return LifecycleResult("blocked", "Human requested changes")
    if github_ops.pr_has_label(pr_number, loop_config.needs_human_review_label, cwd=repo_root):
        return LifecycleResult("blocked", "PR parked for human review")

    changed = github_ops.pr_changed_files(pr_number, cwd=repo_root)
    protected = _protected_paths(changed, repo)
    if protected:
        park_for_human(
            pr_number,
            f"Touches protected paths: {', '.join(protected[:5])}",
            repo_root=repo_root,
            label=loop_config.needs_human_review_label,
        )
        return LifecycleResult("blocked", "Protected paths touched")

    if loop_config.tiered_merge_gate:
        comments = github_ops.pr_comments(pr_number, cwd=repo_root)
        pr_state = get_pr_state(
            load_state(state_path(repo_root)),
            pr_number,
        )
        review_addressed = bool(pr_state.get("review_addressed"))
        risk = None if review_addressed else parse_review_risk(comments)
        oos = _merge_scope_out_of_scope(persona, changed, repo)
        allowed, reason = tiered_merge_allowed(
            ci_green=True,
            risk=risk,
            out_of_scope=oos,
            parked=False,
        )
        if not allowed:
            park_for_human(
                pr_number,
                reason,
                repo_root=repo_root,
                label=loop_config.needs_human_review_label,
            )
            return LifecycleResult("blocked", reason)

    subject = f"[Fleet/{persona}] #{pr_number}"
    body = f"Squash merge via agent-fleet PR loop.\n\n{_AGENT_FOOTER} persona={persona}"
    merged = github_ops.merge_pr(pr_number, subject=subject, body=body, cwd=repo_root)
    if merged:
        return LifecycleResult("merged", "PR merged")
    return LifecycleResult("merge_error", "gh pr merge failed")


def run_pr_lifecycle(
    *,
    pr_number: int,
    branch: str,
    repo: RepoConfig,
    loop_config: PrLoopConfig,
    fleet_config: FleetConfig | None = None,
    worktree: Path | None = None,
    skip_review_wait: bool = False,
    persona: str | None = None,
) -> LifecycleResult:
    """Run address-review → CI wait/fix → merge for one PR."""
    fleet_config = fleet_config or load_fleet_config()
    persona = persona or persona_from_branch(branch, repo.default_persona)
    fleet_log = FleetLogger.for_background(
        run_id=f"pr-loop-{pr_number}",
        persona=persona,
    )
    with fleet_log.bind():
        fleet_log.emit(
            "pr_loop.start",
            pr_number=pr_number,
            branch=branch,
            skip_review_wait=skip_review_wait,
        )
        try:
            result = _run_pr_lifecycle_body(
                pr_number=pr_number,
                branch=branch,
                repo=repo,
                loop_config=loop_config,
                fleet_config=fleet_config,
                worktree=worktree,
                skip_review_wait=skip_review_wait,
                persona=persona,
                fleet_log=fleet_log,
            )
        except Exception as exc:
            fleet_log.emit("pr_loop.error", level="error", error=str(exc))
            logger.exception("PR loop failed for #%s", pr_number)
            raise
        fleet_log.emit(
            "pr_loop.end",
            status=result.status,
            detail=result.detail[:500] if result.detail else "",
        )
        return result


def _run_pr_lifecycle_body(
    *,
    pr_number: int,
    branch: str,
    repo: RepoConfig,
    loop_config: PrLoopConfig,
    fleet_config: FleetConfig,
    worktree: Path | None,
    skip_review_wait: bool,
    persona: str,
    fleet_log: FleetLogger,
) -> LifecycleResult:
    """Inner PR lifecycle implementation (expects bound FleetLogger)."""
    repo_root = repo.repo_root
    pr_config = repo.pr_review
    marker = pr_config.comment_title if pr_config else "Composer PR Analysis"

    wt = worktree
    if wt is None:
        head_ref = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if head_ref.returncode == 0 and head_ref.stdout.strip() == branch:
            wt = repo_root
        else:
            from agent_fleet.pr_loop.worktree import resolve_worktree_path

            base = repo.worktree_base or Path("/tmp/agent-fleet-loop")
            wt = resolve_worktree_path(branch, repo_root=repo_root, worktree_base=base)
            logger.info("Resolved worktree for %s → %s", branch, wt)

    review_body: str | None = find_reviewer_comment(
        github_ops.pr_comments(pr_number, cwd=repo_root),
        marker=marker,
    )
    if review_body is None and not skip_review_wait:
        review_body = poll_for_review_comment(
            pr_number,
            repo_root=repo_root,
            marker=marker,
            timeout_s=loop_config.review_poll_timeout_s,
            poll_s=loop_config.review_poll_s,
        )

    needs_fix = bool(
        review_body
        and has_blocking_findings(
            review_body,
            deletion_only=_diff_is_deletion_only(github_ops.pr_diff(pr_number, cwd=repo_root)),
        )
    )
    if needs_fix:
        prior = get_pr_state(load_state(state_path(repo_root)), pr_number)
        if prior.get("review_addressed"):
            needs_fix = False

    if needs_fix and review_body:
        fleet_log.emit("pr_loop.review_fix.start", pr_number=pr_number)
        wt = github_ops.checkout_branch(branch, wt, repo_root=repo_root)
        fix_attempts = 0
        while fix_attempts < loop_config.max_fix_attempts:
            fix_attempts += 1
            address = address_review_findings(
                pr_number=pr_number,
                branch=branch,
                review_body=review_body,
                repo=repo,
                loop_config=loop_config,
                fleet_config=fleet_config,
                worktree=wt,
            )
            if address.status in {"no_findings", "addressed"}:
                state_file = state_path(repo_root)
                state = load_state(state_file)
                entry = get_pr_state(state, pr_number)
                set_pr_state(
                    state,
                    pr_number,
                    {**entry, "review_addressed": address.status == "addressed"},
                )
                save_state(state_file, state)
                break
            if address.status == "ignored":
                park_for_human(
                    pr_number,
                    "Review findings were not addressed.",
                    repo_root=repo_root,
                    label=loop_config.needs_human_review_label,
                )
                return LifecycleResult("parked", address.detail)
            if address.status in {"scope_violation", "fix_failed"}:
                fleet_log.emit(
                    "pr_loop.review_fix.attempt",
                    level="warning",
                    attempt=fix_attempts,
                    max_attempts=loop_config.max_fix_attempts,
                    status=address.status,
                    detail=address.detail[:500] if address.detail else "",
                )
                logger.warning(
                    "Review fix attempt %s/%s PR #%s: %s — %s",
                    fix_attempts,
                    loop_config.max_fix_attempts,
                    pr_number,
                    address.status,
                    address.detail,
                )
            if address.status == "scope_violation":
                followup = _file_scope_violation_followup(
                    pr_number=pr_number,
                    branch=branch,
                    violation_detail=address.detail,
                    review_body=review_body,
                    repo=repo,
                )
                followup_ref = f"#{followup}" if followup else "(filing failed; see watcher log)"
                github_ops.post_pr_comment(
                    (
                        "Review-fix path detected findings that belong outside this PR's "
                        f"scope. Continuing to CI fix; follow-up issue: {followup_ref}.\n\n"
                        f"Out-of-scope detail: `{address.detail}`"
                    ),
                    pr_number,
                    cwd=repo_root,
                )
                logger.info(
                    "Review fix scope_violation PR #%s: filed follow-up %s; continuing to CI",
                    pr_number,
                    followup,
                )
                break
            if fix_attempts >= loop_config.max_fix_attempts:
                if address.status == "fix_failed":
                    park_for_human(
                        pr_number,
                        f"Automated review fix failed: {address.detail}",
                        repo_root=repo_root,
                        label=loop_config.needs_human_review_label,
                    )
                    return LifecycleResult("parked", address.detail)
                return address
            review_body = (
                poll_for_review_comment(
                    pr_number,
                    repo_root=repo_root,
                    marker=marker,
                    timeout_s=loop_config.review_poll_timeout_s,
                    poll_s=loop_config.review_poll_s,
                )
                or review_body
            )

    ci_fix_attempts = 0
    while True:
        fleet_log.emit("pr_loop.ci.wait", pr_number=pr_number, attempt=ci_fix_attempts + 1)
        ci = wait_for_ci_green(
            pr_number,
            repo_root=repo_root,
            loop_config=loop_config,
        )
        if ci.status == "ci_green":
            fleet_log.emit("pr_loop.ci.green", pr_number=pr_number)
            break
        if ci.status != "ci_failed" or ci_fix_attempts >= loop_config.max_ci_fix_attempts:
            fleet_log.emit(
                "pr_loop.ci.end", status=ci.status, detail=ci.detail[:500] if ci.detail else ""
            )
            return ci
        ci_fix_attempts += 1
        fleet_log.emit(
            "pr_loop.ci.fix",
            pr_number=pr_number,
            attempt=ci_fix_attempts,
            max_attempts=loop_config.max_ci_fix_attempts,
        )
        _all, _pending, failed = github_ops.pr_checks(
            pr_number,
            cwd=repo_root,
            ignored=loop_config.ignored_ci_checks,
        )
        failed_names = [str(c.get("name", "")) for c in failed]
        wt = github_ops.checkout_branch(branch, wt, repo_root=repo_root)
        fixed = attempt_ci_fix(
            pr_number=pr_number,
            branch=branch,
            failed_checks=failed_names,
            repo=repo,
            loop_config=loop_config,
            fleet_config=fleet_config,
            worktree=wt,
            persona=persona,
        )
        if not fixed:
            return LifecycleResult("ci_failed", "CI fix attempt produced no push")
        time.sleep(loop_config.post_fix_poll_s)

    if not loop_config.auto_merge:
        fleet_log.emit("pr_loop.ready", auto_merge=False)
        return LifecycleResult("ready", "CI green; auto_merge disabled")

    fleet_log.emit("pr_loop.merge.attempt", pr_number=pr_number)
    return try_merge(
        pr_number=pr_number,
        persona=persona,
        repo=repo,
        loop_config=loop_config,
    )
