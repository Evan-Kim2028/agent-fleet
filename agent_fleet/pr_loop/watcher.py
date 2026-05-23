"""Poll open fleet PRs and drive review-fix-merge lifecycle."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from agent_fleet.config import load_fleet_config
from agent_fleet.pr_loop import github_ops
from agent_fleet.pr_loop.lifecycle import run_pr_lifecycle
from agent_fleet.pr_loop.review_parse import find_reviewer_comment
from agent_fleet.pr_loop.state import (
    get_pr_state,
    load_state,
    merge_cooldown_remaining,
    save_state,
    set_pr_state,
    state_path,
)

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.config import FleetConfig
    from agent_fleet.pr_loop.config import PrLoopConfig
    from agent_fleet.repo import RepoConfig

logger = logging.getLogger(__name__)


class PrLoopWatcher:
    """Long-running watcher for fleet PR lifecycle automation."""

    def __init__(
        self,
        repo: RepoConfig,
        loop_config: PrLoopConfig,
        fleet_config: FleetConfig | None = None,
    ) -> None:
        self.repo = repo
        self.loop_config = loop_config
        self.fleet_config = fleet_config or load_fleet_config()
        self.state_file = state_path(repo.repo_root, loop_config.state_file)

    def poll_once(self) -> list[dict[str, str]]:
        state = load_state(self.state_file)
        remaining = merge_cooldown_remaining(state, self.loop_config.merge_cooldown_s)
        if remaining > 0:
            logger.info("Merge cooldown active (%.0fs remaining)", remaining)
            return [{"status": "cooldown", "detail": f"{remaining:.0f}s"}]

        results: list[dict[str, str]] = []
        prs = github_ops.list_open_fleet_prs(
            branch_prefixes=self.loop_config.branch_prefixes,
            cwd=self.repo.repo_root,
        )
        if not prs:
            return results

        marker = (
            self.repo.pr_review.comment_title
            if self.repo.pr_review
            else "Composer PR Analysis"
        )

        for pr in prs:
            pr_number = int(pr["number"])
            branch = str(pr.get("headRefName") or "")
            entry = get_pr_state(state, pr_number)

            if entry.get("merged"):
                continue
            if entry.get("parked"):
                continue

            if github_ops.pr_has_label(
                pr_number,
                self.loop_config.needs_human_review_label,
                cwd=self.repo.repo_root,
            ):
                set_pr_state(state, pr_number, {**entry, "parked": True})
                continue

            _all, pending, failed = github_ops.pr_checks(
                pr_number,
                cwd=self.repo.repo_root,
                ignored=self.loop_config.ignored_ci_checks,
            )

            comments = github_ops.pr_comments(pr_number, cwd=self.repo.repo_root)
            review_body = find_reviewer_comment(comments, marker=marker)
            fix_attempts = int(entry.get("fix_attempts") or 0)

            needs_fix = False
            if review_body and not entry.get("review_addressed"):
                from agent_fleet.pr_loop.lifecycle import _diff_is_deletion_only
                from agent_fleet.pr_loop.review_parse import has_blocking_findings

                needs_fix = has_blocking_findings(
                    review_body,
                    deletion_only=_diff_is_deletion_only(
                        github_ops.pr_diff(pr_number, cwd=self.repo.repo_root)
                    ),
                )

            if pending and not needs_fix and not failed:
                results.append({"pr": str(pr_number), "status": "ci_pending"})
                continue

            if failed and not needs_fix and fix_attempts >= self.loop_config.max_ci_fix_attempts:
                results.append({"pr": str(pr_number), "status": "ci_failed"})
                continue

            if not review_body and not failed and not pending:
                results.append({"pr": str(pr_number), "status": "awaiting_review"})
                continue

            logger.info("Running lifecycle for PR #%s (%s)", pr_number, branch)
            outcome = run_pr_lifecycle(
                pr_number=pr_number,
                branch=branch,
                repo=self.repo,
                loop_config=self.loop_config,
                fleet_config=self.fleet_config,
                skip_review_wait=True,
            )

            new_entry = {
                **entry,
                "last_status": outcome.status,
                "last_detail": outcome.detail,
                "fix_attempts": fix_attempts + (1 if outcome.status == "addressed" else 0),
            }
            if outcome.status == "merged":
                new_entry["merged"] = True
                state["last_merge_ts"] = time.time()
            if outcome.status == "parked":
                new_entry["parked"] = True
            set_pr_state(state, pr_number, new_entry)
            save_state(self.state_file, state)

            results.append(
                {
                    "pr": str(pr_number),
                    "status": outcome.status,
                    "detail": outcome.detail,
                }
            )

            if outcome.status == "merged":
                break

        save_state(self.state_file, state)
        return results

    def run_forever(self) -> None:
        logger.info(
            "PR loop watcher started for %s (poll=%ss)",
            self.repo.display_name,
            self.loop_config.poll_interval_s,
        )
        while True:
            try:
                outcomes = self.poll_once()
                for item in outcomes:
                    logger.info("PR loop: %s", item)
            except Exception:
                logger.exception("PR loop poll failed")
            time.sleep(self.loop_config.poll_interval_s)


def run_watcher_once(workspace: Path) -> list[dict[str, str]]:
    from agent_fleet.pr_loop.config import load_pr_loop_config
    from agent_fleet.repo import find_repo_config

    repo = find_repo_config(workspace)
    if repo is None:
        raise RuntimeError(f"No .agent-fleet.yaml found under {workspace}")
    raw = {}
    import yaml

    for name in (".agent-fleet.yaml", ".agent-fleet.yml"):
        path = repo.repo_root / name
        if path.exists():
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            break
    loop_config = load_pr_loop_config(repo.repo_root, raw)
    if loop_config is None or not loop_config.enabled:
        raise RuntimeError("pr_loop.enabled is not true in .agent-fleet.yaml")
    watcher = PrLoopWatcher(repo, loop_config)
    return watcher.poll_once()
