"""Run a single issue-triggered fleet dispatch."""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from pathlib import Path

from agent_fleet.backends import make_backend
from agent_fleet.capacity import FleetCapacity, is_visual_audit_dispatch
from agent_fleet.config import load_fleet_config
from agent_fleet.integrations.command_verifier import CommandVerifier
from agent_fleet.integrations.github_forge import GitHubForge
from agent_fleet.integrations.local_git import LocalGitOps
from agent_fleet.issue_loop import github_ops
from agent_fleet.issue_loop.config import IssueDispatchConfig
from agent_fleet.issue_loop.triggers import extract_persona
from agent_fleet.memory import memory_snapshot
from agent_fleet.personas import YamlPersonaResolver
from agent_fleet.repo import find_repo_config
from agent_fleet.runner import FleetRunConfig, LocalFleetRunner, _spine_from_repo

logger = logging.getLogger(__name__)


def _build_pr_body(*, run_id: str, issue_number: int, summary: str | None) -> str:
    parts = [f"Automated fleet PR. Run: `{run_id}`"]
    if summary:
        parts.append(f"\n## Summary\n\n{summary}")
    parts.append("\n## Test plan\n\nSee implementation brief and CI checks.")
    parts.append(f"\nCloses #{issue_number}")
    return "\n".join(parts)


def run_issue_dispatch(
    *,
    issue_number: int,
    comment_body: str,
    repo_root: Path,
    persona: str | None = None,
    fleet_config_path: str | None = None,
    dispatch_config: IssueDispatchConfig | None = None,
) -> int:
    """Execute full pipeline for an issue trigger. Returns process exit code."""
    from agent_fleet.logging_config import configure_fleet_logging

    configure_fleet_logging()

    repo = find_repo_config(repo_root)
    if repo is None:
        logger.error("No .agent-fleet.yaml found under %s", repo_root)
        return 1

    dispatch = dispatch_config or IssueDispatchConfig()
    resolved_persona = persona or extract_persona(comment_body, dispatch.trigger_pattern)
    if not resolved_persona:
        logger.error("Could not extract persona from comment")
        return 1

    fleet_config = load_fleet_config(fleet_config_path)
    if repo.personas_dir:
        fleet_config.personas_dir = repo.personas_dir

    mutex_label = f"{dispatch.mutex_label_prefix}/{issue_number}"
    running_label = f"{dispatch.running_label_prefix}/{resolved_persona}"

    try:
        github_ops.add_label(issue_number, mutex_label, cwd=repo.repo_root)
        github_ops.add_label(issue_number, running_label, cwd=repo.repo_root)
    except Exception as exc:
        logger.warning("Failed to add mutex labels: %s", exc)

    issue = github_ops.issue_view(issue_number, cwd=repo.repo_root)
    title = str(issue.get("title") or f"Issue #{issue_number}")
    body = str(issue.get("body") or "")
    issue_labels = github_ops.issue_labels(issue_number, cwd=repo.repo_root)
    is_visual_audit = is_visual_audit_dispatch(issue_labels=issue_labels, title=title, body=body)
    if is_visual_audit:
        memory_snapshot(label=f"dispatch start issue #{issue_number}")
    if comment_body.strip():
        body = f"{body}\n\n---\nDispatch trigger:\n{comment_body}"

    status_comment = (
        f"Fleet dispatch started for `{resolved_persona}` (issue #{issue_number}).\n\n"
        f"{dispatch.comment_marker}"
    )
    try:
        github_ops.post_issue_comment(issue_number, status_comment, cwd=repo.repo_root)
    except Exception as exc:
        logger.warning("Failed to post start comment: %s", exc)

    spine = _spine_from_repo(repo)
    branch_prefix = spine.branch_prefix
    git_ops = LocalGitOps(
        repo.repo_root,
        use_worktree=True,
        worktree_base=spine.worktree_base,
    )
    capacity = repo.capacity or FleetCapacity.defaults()
    run_capacity = capacity.run
    runner = LocalFleetRunner(
        backend=make_backend(fleet_config),
        persona_resolver=YamlPersonaResolver(fleet_config),
        git_ops=git_ops,
        verifier=CommandVerifier(repo),
        spine=spine,
        config=FleetRunConfig(
            create_branch=True,
            commit_changes=True,
            resume=True,
            max_research_workers=run_capacity.max_research_workers,
            max_verify_retries=run_capacity.max_verify_retries,
            memory_limit_parent=run_capacity.memory_limit_parent,
            memory_limit_research=run_capacity.memory_limit_research,
        ),
        forge=GitHubForge(cwd=repo.repo_root),
        fleet_config=fleet_config,
    )

    result = runner.run(
        task_id=issue_number,
        title=title,
        body=body,
        persona=resolved_persona,
        repo_root=repo.repo_root,
        base_branch=repo.default_branch,
        pr_title=f"{branch_prefix}/{resolved_persona}/#{issue_number}",
        pr_body_builder=lambda run_id, summary: _build_pr_body(
            run_id=run_id,
            issue_number=issue_number,
            summary=summary,
        ),
        pr_labels=[spine.pr_ready_label],
        issue_number=issue_number,
        issue_labels=issue_labels,
    )

    if result.pr_number:
        done_msg = (
            f"Fleet run `{result.run_id}` complete — PR #{result.pr_number} "
            f"(`{result.branch_name}`).\n\nOutcome: `{result.outcome}`\n\n"
            f"{dispatch.comment_marker}"
        )
    else:
        done_msg = (
            f"Fleet run `{result.run_id}` finished with outcome `{result.outcome}`.\n\n"
            f"{result.error or result.summary or 'No details.'}\n\n"
            f"{dispatch.comment_marker}"
        )

    try:
        github_ops.post_issue_comment(issue_number, done_msg, cwd=repo.repo_root)
    except Exception as exc:
        logger.warning("Failed to post completion comment: %s", exc)
    finally:
        if is_visual_audit:
            memory_snapshot(label=f"dispatch end issue #{issue_number}")
        for label in (mutex_label, running_label):
            with contextlib.suppress(Exception):
                github_ops.remove_label(issue_number, label, cwd=repo.repo_root)

    return 0 if result.outcome in {"completed", "review_changes_requested"} else 1


def main() -> None:
    issue_number = int(os.environ.get("ISSUE_NUMBER", "0"))
    comment_body = os.environ.get("COMMENT_BODY", "")
    persona = os.environ.get("PERSONA") or None
    workspace = Path(os.environ.get("AGENT_FLEET_WORKSPACE", Path.cwd())).resolve()
    fleet_config_path = os.environ.get("AGENT_FLEET_CONFIG")

    if issue_number <= 0:
        print("ISSUE_NUMBER env var required", file=sys.stderr)
        raise SystemExit(1)

    raise SystemExit(
        run_issue_dispatch(
            issue_number=issue_number,
            comment_body=comment_body,
            repo_root=workspace,
            persona=persona,
            fleet_config_path=fleet_config_path,
        )
    )


if __name__ == "__main__":
    main()
