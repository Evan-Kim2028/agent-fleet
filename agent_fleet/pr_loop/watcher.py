"""Poll open fleet PRs and drive review-fix-merge lifecycle."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, cast

from agent_fleet.config import load_fleet_config
from agent_fleet.pr_loop import github_ops
from agent_fleet.pr_loop.lifecycle import run_pr_lifecycle
from agent_fleet.pr_loop.review_parse import find_reviewer_comment
from agent_fleet.pr_loop.worktree import sweep_orphan_worktrees
from agent_fleet.state import (
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


def _entry_int(entry: dict[str, object], key: str) -> int:
    value = entry.get(key)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


def maybe_unpark_pr_entry(
    entry: dict[str, object],
    *,
    head_ref_oid: str,
) -> dict[str, object]:
    """Clear parked when the PR branch advances (new commits pushed)."""
    updated = dict(entry)
    previous_oid = str(entry.get("last_head_oid") or "")
    if head_ref_oid:
        updated["last_head_oid"] = head_ref_oid
    if not entry.get("parked"):
        return updated
    if head_ref_oid and previous_oid and head_ref_oid != previous_oid:
        logger.info(
            "Unparking PR — branch advanced (%s → %s)",
            previous_oid[:8],
            head_ref_oid[:8],
        )
        updated["parked"] = False
        updated.pop("review_addressed", None)
    return updated


def _pr_number(pr: dict[str, object]) -> int:
    number = pr["number"]
    if isinstance(number, int):
        return number
    if isinstance(number, str):
        return int(number)
    raise TypeError(f"PR number must be int or str, got {type(number)!r}")


def prioritize_fleet_prs(
    prs: list[dict[str, object]],
    state: dict[str, object],
    *,
    fleet_ready_label: str = "fleet-ready",
) -> list[dict[str, object]]:
    """Sort open fleet PRs: ready + newest first; deprioritize parked/merged entries."""

    def score(pr: dict[str, object]) -> int:
        pr_number = _pr_number(pr)
        entry = get_pr_state(state, pr_number)
        if entry.get("merged"):
            return -10_000
        if entry.get("parked"):
            return -5_000
        value = pr_number
        labels_raw = pr.get("labels")
        label_names: set[str] = set()
        if isinstance(labels_raw, list):
            for item in labels_raw:
                if isinstance(item, dict):
                    name = cast("dict[str, object]", item).get("name")
                    label_names.add(str(name or ""))
        if fleet_ready_label in label_names:
            value += 10_000
        if pr.get("isDraft"):
            value -= 500
        return value

    return sorted(prs, key=score, reverse=True)


class PrLoopWatcher:
    """Long-running watcher for fleet PR lifecycle automation."""

    def __init__(
        self,
        repo: RepoConfig,
        loop_config: PrLoopConfig,
        fleet_config: FleetConfig | None = None,
        *,
        state_root: Path | None = None,
    ) -> None:
        self.repo = repo
        self.loop_config = loop_config
        self.fleet_config = fleet_config or load_fleet_config()
        from agent_fleet.repo import fleet_state_root

        self.state_file = state_path(state_root or fleet_state_root(repo))

    def poll_once(self) -> list[dict[str, str]]:
        state = load_state(self.state_file)
        remaining = merge_cooldown_remaining(state, self.loop_config.merge_cooldown_s)
        if remaining > 0:
            logger.info(
                "Merge cooldown active (%.0fs remaining); review/fix still runs",
                remaining,
            )

        results: list[dict[str, str]] = []
        prs = github_ops.list_open_fleet_prs(
            branch_prefixes=self.loop_config.branch_prefixes,
            cwd=self.repo.repo_root,
        )

        # Sweep orphan worktrees (no matching open PR) once per poll.
        active_branches = {str(pr.get("headRefName") or "") for pr in prs}
        try:
            from pathlib import Path as _Path

            from agent_fleet.spine_config import SpineConfig as _SpineConfig

            _wt_base = self.repo.worktree_base
            if _wt_base is None:
                _raw = self.repo.spine_overrides.get("worktree_base")
                if _raw:
                    _wt_base = _Path(str(_raw)).expanduser()
            if _wt_base is None:
                _wt_base = _SpineConfig.defaults().worktree_base
            sweep_orphan_worktrees(self.repo.repo_root, _wt_base, active_branches)
        except Exception:
            logger.debug("Worktree sweep failed", exc_info=True)

        if not prs:
            return results

        ready_label = str(self.repo.spine_overrides.get("pr_ready_label") or "fleet-ready")
        prs = prioritize_fleet_prs(
            prs,
            state,
            fleet_ready_label=ready_label,
        )

        marker = (
            self.repo.pr_review.comment_title if self.repo.pr_review else "Composer PR Analysis"
        )

        for pr in prs:
            pr_number = _pr_number(pr)
            branch = str(pr.get("headRefName") or "")
            head_ref_oid = str(pr.get("headRefOid") or "")
            base_ref_name = str(pr.get("baseRefName") or "")
            entry = get_pr_state(state, pr_number)
            entry = maybe_unpark_pr_entry(entry, head_ref_oid=head_ref_oid)
            if head_ref_oid:
                set_pr_state(state, pr_number, entry)

            if entry.get("merged"):
                continue
            if entry.get("parked"):
                continue

            _all, pending, failed = github_ops.pr_checks(
                pr_number,
                cwd=self.repo.repo_root,
                ignored=self.loop_config.ignored_ci_checks,
            )

            comments = github_ops.pr_comments(pr_number, cwd=self.repo.repo_root)
            review_body = find_reviewer_comment(comments, marker=marker)
            fix_attempts = _entry_int(entry, "fix_attempts")
            ci_timeout_attempts = _entry_int(entry, "ci_timeout_attempts")

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
                base_ref_name=base_ref_name,
            )

            new_ci_timeout_attempts = (
                ci_timeout_attempts + 1 if outcome.status == "ci_timeout" else 0
            )
            new_entry = {
                **entry,
                "last_status": outcome.status,
                "last_detail": outcome.detail,
                "fix_attempts": fix_attempts + (1 if outcome.status == "addressed" else 0),
                "ci_timeout_attempts": new_ci_timeout_attempts,
            }
            if head_ref_oid:
                new_entry["last_head_oid"] = head_ref_oid
            if outcome.status == "merged":
                new_entry["merged"] = True
                state["last_merge_ts"] = time.time()
            if outcome.status in {"parked", "blocked"}:
                new_entry["parked"] = True
            if (
                outcome.status == "ci_timeout"
                and new_ci_timeout_attempts >= self.loop_config.max_ci_timeout_attempts
            ):
                new_entry["parked"] = True
                new_entry["last_detail"] = (
                    f"Parked after {self.loop_config.max_ci_timeout_attempts} "
                    "consecutive ci_timeout outcomes"
                )
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
