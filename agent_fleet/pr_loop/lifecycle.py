"""Address review findings and wait for CI + merge."""

from __future__ import annotations

import logging
import re
import subprocess
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_fleet.agent_mode import parse_agent_mode
from agent_fleet.backends import make_backend
from agent_fleet.config import FleetConfig, load_fleet_config
from agent_fleet.personas import YamlPersonaResolver
from agent_fleet.pr_loop import github_ops
from agent_fleet.pr_loop.review_parse import (
    find_reviewer_comment,
    has_blocking_findings,
    parse_review_risk,
)
from agent_fleet.repo import RepoConfig, merge_repo_into_fleet_config
from agent_fleet.scope import files_outside_allowed_paths

if TYPE_CHECKING:
    from agent_fleet.pr_loop.config import PrLoopConfig

logger = logging.getLogger(__name__)

_AGENT_FOOTER = "\U0001F916 Agent:"
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
    if len(parts) >= 3 and parts[0] == "fleet":
        return parts[1]
    if len(parts) >= 2 and parts[0] == "fleet" and parts[1].startswith("task-"):
        return default_persona
    return default_persona


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


def poll_for_review_comment(
    pr_number: int,
    *,
    repo_root: Path,
    marker: str,
    timeout_s: int,
    poll_s: int = 30,
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
    persona: str,
) -> LifecycleResult:
    if not has_blocking_findings(
        review_body,
        deletion_only=_diff_is_deletion_only(github_ops.pr_diff(pr_number, cwd=repo.repo_root)),
    ):
        return LifecycleResult("no_findings", "Review has no blocking findings")

    fix_persona_name = loop_config.fix_persona or persona or repo.default_persona
    config = merge_repo_into_fleet_config(fleet_config, repo)
    resolver = YamlPersonaResolver(config)
    persona_obj = resolver.load(fix_persona_name)
    backend = make_backend(config)

    verify_block = ""
    if repo.verify_commands:
        verify_block = "\n".join(f"- `{cmd}`" for cmd in repo.verify_commands)

    prompt = textwrap.dedent(f"""\
        The PR analyzer posted review findings on PR #{pr_number}. Address every
        blocking finding in the review comment below.

        ## Review
        {review_body}

        ## Instructions
        1. Read each finding and fix valid issues in the relevant files.
        2. Stay within your persona scope.
        3. Run verify commands before finishing:
        {verify_block or "- (none configured)"}
        4. Do NOT commit — the orchestrator commits after this phase.
        5. If a finding is a false positive, note it but do not change code for it.
    """)

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
        return LifecycleResult("fix_failed", result.stderr or "Fix agent failed")

    allowed = persona_obj.allowed_paths or list(
        repo.persona_scope_allowlist.get(fix_persona_name, ())
    )
    if allowed:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=worktree,
            check=False,
        )
        changed = [
            line[3:].strip()
            for line in status.stdout.splitlines()
            if line.strip() and len(line) > 3
        ]
        violating = files_outside_allowed_paths(tuple(allowed), changed)
        if violating:
            return LifecycleResult("scope_violation", f"Out of scope: {violating}")

    message = (
        f"fix(fleet): address PR review feedback\n\n"
        f"{_AGENT_FOOTER} persona={fix_persona_name} | PR #{pr_number}"
    )
    pushed = github_ops.commit_and_push(worktree, message, branch)
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
            time.sleep(15)
            continue
        if failed:
            names = [str(c.get("name", "")) for c in failed]
            return LifecycleResult("ci_failed", f"Failed checks: {names}")
        if not pending:
            return LifecycleResult("ci_green", "All checks passed")
        time.sleep(30)
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
    fix_persona_name = loop_config.fix_persona or persona or repo.default_persona
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

        Do NOT commit — the orchestrator commits after this phase.
        Do NOT weaken CI workflows to make checks pass.
    """)
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
        f"fix(fleet): CI failures on PR #{pr_number}\n\n"
        f"{_AGENT_FOOTER} persona={fix_persona_name}"
    )
    return github_ops.commit_and_push(worktree, message, branch)


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
        risk = parse_review_risk(comments)
        allowed_paths = repo.persona_scope_allowlist.get(persona, ())
        oos = (
            list(files_outside_allowed_paths(allowed_paths, changed))
            if allowed_paths
            else []
        )
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
) -> LifecycleResult:
    """Run address-review → CI wait/fix → merge for one PR."""
    fleet_config = fleet_config or load_fleet_config()
    repo_root = repo.repo_root
    persona = persona_from_branch(branch, repo.default_persona)
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
            safe_branch = re.sub(r"[^\w.-]+", "_", branch)
            base = repo.worktree_base or Path("/tmp/agent-fleet-loop")
            wt = Path(base) / safe_branch

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
        )

    needs_fix = bool(
        review_body
        and has_blocking_findings(
            review_body,
            deletion_only=_diff_is_deletion_only(
                github_ops.pr_diff(pr_number, cwd=repo_root)
            ),
        )
    )
    if needs_fix:
        pr_state_file = repo_root / loop_config.state_file
        from agent_fleet.pr_loop.state import get_pr_state, load_state

        prior = get_pr_state(load_state(pr_state_file), pr_number)
        if prior.get("review_addressed"):
            needs_fix = False

    if needs_fix and review_body:
        github_ops.checkout_branch(branch, wt, repo_root=repo_root)
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
                persona=persona,
            )
            if address.status in {"no_findings", "addressed"}:
                from agent_fleet.pr_loop.state import get_pr_state, load_state, save_state, set_pr_state

                state_file = repo_root / loop_config.state_file
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
            if fix_attempts >= loop_config.max_fix_attempts:
                return address
            review_body = poll_for_review_comment(
                pr_number,
                repo_root=repo_root,
                marker=marker,
                timeout_s=loop_config.review_poll_timeout_s,
            ) or review_body

    ci_fix_attempts = 0
    while True:
        ci = wait_for_ci_green(
            pr_number,
            repo_root=repo_root,
            loop_config=loop_config,
        )
        if ci.status == "ci_green":
            break
        if ci.status != "ci_failed" or ci_fix_attempts >= loop_config.max_ci_fix_attempts:
            return ci
        ci_fix_attempts += 1
        _all, _pending, failed = github_ops.pr_checks(
            pr_number,
            cwd=repo_root,
            ignored=loop_config.ignored_ci_checks,
        )
        failed_names = [str(c.get("name", "")) for c in failed]
        github_ops.checkout_branch(branch, wt, repo_root=repo_root)
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
        time.sleep(60)

    if not loop_config.auto_merge:
        return LifecycleResult("ready", "CI green; auto_merge disabled")

    return try_merge(
        pr_number=pr_number,
        persona=persona,
        repo=repo,
        loop_config=loop_config,
    )
