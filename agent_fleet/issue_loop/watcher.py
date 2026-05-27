"""Poll GitHub issue comments for /agent --persona triggers."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from agent_fleet.capacity import (
    RETRYABLE_ADMISSION_REASONS,
    FleetCapacity,
    FleetCapacityGate,
    count_visual_in_flight,
    is_visual_audit_dispatch,
)
from agent_fleet.in_flight import reap_in_flight
from agent_fleet.issue_loop import github_ops
from agent_fleet.issue_loop import queue as issue_queue
from agent_fleet.issue_loop.backlog_dispatcher import BacklogDispatcher
from agent_fleet.issue_loop.triggers import (
    extract_issue_number,
    extract_persona,
    is_stop_command,
    is_watcher_comment,
)
from agent_fleet.memory import (
    available_ram_gb,
    cleanup_playwright_mcp_processes,
    count_playwright_mcp_processes,
    memory_snapshot,
)
from agent_fleet.observability.fleet_logger import emit_fleet_event
from agent_fleet.repo import (
    RepoConfig,
    find_repo_config,
    fleet_state_root,
    iter_target_repos,
    target_registry,
)
from agent_fleet.state import (
    apply_issue_defaults,
    load_state,
    now_iso,
    save_state,
    state_path,
)

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.issue_loop.config import IssueDispatchConfig
    from agent_fleet.pr_loop.config import PrLoopConfig
    from agent_fleet.schedule.config import ScheduleConfig

logger = logging.getLogger(__name__)

_shutdown_requested = False


def _on_sigterm(_signum: int, _frame: object) -> None:
    global _shutdown_requested
    _shutdown_requested = True


def _dispatch_executable() -> list[str]:
    return [sys.executable, "-m", "agent_fleet.issue_loop.dispatch"]


def _cleanup_orphaned_playwright_mcp(state: dict[str, Any]) -> None:
    """Force-kill lingering Playwright MCP processes when no visual audits are active."""
    if count_visual_in_flight(state) > 0:
        return
    before = count_playwright_mcp_processes()
    if before == 0:
        return
    logger.warning("Orphaned playwright MCP processes detected: count=%s", before)
    result = cleanup_playwright_mcp_processes(force_kill=True)
    emit_fleet_event(
        "mcp.orphan.cleanup",
        level="warning" if result.after > 0 else "info",
        before=result.before,
        after=result.after,
        force_killed=list(result.force_killed),
        cleaned=result.cleaned,
    )
    logger.info(
        "Orphan playwright MCP cleanup: before=%s after=%s force_killed=%s",
        result.before,
        result.after,
        list(result.force_killed),
    )


def _spawn_dispatch(
    *,
    issue_number: int,
    comment_body: str,
    persona: str,
    repo_root: Path,
    target_config_path: Path | None = None,
) -> int | None:
    env = os.environ.copy()
    env["ISSUE_NUMBER"] = str(issue_number)
    env["COMMENT_BODY"] = comment_body
    env["PERSONA"] = persona
    env["AGENT_FLEET_WORKSPACE"] = str(repo_root)
    if target_config_path is not None:
        env["AGENT_FLEET_TARGET_CONFIG"] = str(target_config_path)
    proc = subprocess.Popen(
        _dispatch_executable(),
        env=env,
        cwd=str(repo_root),
        start_new_session=True,
    )
    return proc.pid


class IssueLoopWatcher:
    """Long-running watcher for /agent issue comment triggers."""

    def __init__(
        self,
        repo: RepoConfig,
        dispatch_config: IssueDispatchConfig,
        *,
        state_root: Path | None = None,
    ) -> None:
        self.repo = repo
        self.config = dispatch_config
        self.state_file = state_path(state_root or fleet_state_root(repo))
        capacity = repo.capacity or FleetCapacity.defaults()
        self.capacity_gate = FleetCapacityGate(capacity)

    def _spawn_dispatch(
        self,
        *,
        issue_number: int,
        comment_body: str,
        persona: str,
        repo_root: Path,
    ) -> int | None:
        return _spawn_dispatch(
            issue_number=issue_number,
            comment_body=comment_body,
            persona=persona,
            repo_root=repo_root,
            target_config_path=self.repo.config_path,
        )

    def poll_once(self, state: dict[str, Any] | None = None) -> list[dict[str, str]]:
        if state is None:
            state = load_state(self.state_file)
        apply_issue_defaults(state)
        reap_in_flight(state)
        _cleanup_orphaned_playwright_mcp(state)
        results: list[dict[str, str]] = []

        repo_name = github_ops.repo_full_name(cwd=self.repo.repo_root)
        comments = github_ops.poll_issue_comments(
            repo_name,
            state["since"],
            cwd=self.repo.repo_root,
        )
        new_since = now_iso()
        retryable_deferred = False

        for comment in comments:
            comment_id = str(comment.get("id", ""))
            if comment_id in state["seen"]:
                continue

            body = str(comment.get("body") or "")
            if is_watcher_comment(body, self.config.comment_marker):
                state["seen"].append(comment_id)
                continue

            issue_url = str(comment.get("issue_url") or "")
            issue_number = extract_issue_number(issue_url)
            if issue_number and is_stop_command(body, self.config.stop_pattern):
                state["seen"].append(comment_id)
                results.append({"issue": str(issue_number), "status": "stop_noted"})
                continue

            persona = extract_persona(body, self.config.trigger_pattern)
            if not persona or not issue_number:
                continue

            issue_labels: list[str] = []
            issue_title = ""
            issue_body = ""
            try:
                issue = github_ops.issue_view(issue_number, cwd=self.repo.repo_root)
                issue_title = str(issue.get("title") or "")
                issue_body = str(issue.get("body") or "")
                issue_labels = github_ops.issue_labels(issue_number, cwd=self.repo.repo_root)
            except Exception as exc:
                logger.warning(
                    "Could not load issue #%s metadata for admission: %s",
                    issue_number,
                    exc,
                )

            is_visual_audit = is_visual_audit_dispatch(
                issue_labels=issue_labels,
                title=issue_title,
                body=issue_body,
            )
            admission = self.capacity_gate.try_admit(
                state,
                issue_number=issue_number,
                persona=persona,
                is_visual_audit=is_visual_audit,
                available_ram_gb=available_ram_gb(),
            )
            if not admission.allowed:
                retryable = admission.reason in RETRYABLE_ADMISSION_REASONS
                if retryable:
                    retryable_deferred = True
                emit_fleet_event(
                    "admission.check",
                    level="warning" if retryable else "info",
                    issue_number=issue_number,
                    persona=persona,
                    allowed=False,
                    reason=admission.reason,
                    retryable=retryable,
                    visual_audit=is_visual_audit,
                )
                if not retryable:
                    state["seen"].append(comment_id)
                results.append(
                    {
                        "issue": str(issue_number),
                        "status": admission.reason,
                    }
                )
                logger.info(
                    "Dispatch deferred: issue #%s persona=%s reason=%s retryable=%s",
                    issue_number,
                    persona,
                    admission.reason,
                    retryable,
                )
                continue

            logger.info(
                "Trigger: issue #%s persona=%s visual_audit=%s",
                issue_number,
                persona,
                is_visual_audit,
            )
            if is_visual_audit:
                memory_snapshot(label=f"pre-dispatch issue #{issue_number}")
            state["seen"].append(comment_id)
            pid = self._spawn_dispatch(
                issue_number=issue_number,
                comment_body=body,
                persona=persona,
                repo_root=self.repo.repo_root,
            )
            if pid is not None:
                in_flight = state.setdefault("in_flight", {}).setdefault(str(issue_number), [])
                in_flight.append(
                    {
                        "pid": pid,
                        "persona": persona,
                        "visual_audit": is_visual_audit,
                    }
                )
                results.append(
                    {
                        "issue": str(issue_number),
                        "status": "dispatched",
                        "pid": str(pid),
                    }
                )

        if not retryable_deferred:
            state["since"] = new_since

        if self.config.queue and self.config.queue.enabled:
            queue_results, queue_deferred = issue_queue.poll_queue(
                repo_root=self.repo.repo_root,
                dispatch_config=self.config,
                queue_config=self.config.queue,
                state=state,
                capacity_gate=self.capacity_gate,
                spawn_dispatch=self._spawn_dispatch,
                available_ram_gb=available_ram_gb(),
                config_root=self.repo.config_root,
            )
            results.extend(queue_results)
            if queue_deferred:
                retryable_deferred = True

        save_state(self.state_file, state)
        return results

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, _on_sigterm)
        state = load_state(self.state_file)
        logger.info(
            "Issue watcher started for %s (interval=%ss)",
            self.repo.display_name,
            self.config.poll_interval_s,
        )
        while not _shutdown_requested:
            try:
                self.poll_once(state)
            except Exception as exc:
                logger.exception("Issue poll error: %s", exc)
            if _shutdown_requested:
                break
            time.sleep(self.config.poll_interval_s)
        save_state(self.state_file, state)


def run_watcher_once(workspace: Path) -> list[dict[str, str]]:
    repo = find_repo_config(workspace)
    if repo is None or repo.issue_dispatch is None or not repo.issue_dispatch.enabled:
        return [{"status": "disabled"}]
    watcher = IssueLoopWatcher(repo, repo.issue_dispatch)
    return watcher.poll_once()


class CombinedWatcher:
    """Issue dispatch, PR loop, and cron schedules in one poll cycle."""

    def __init__(
        self,
        repo: RepoConfig,
        *,
        issue_config: IssueDispatchConfig | None,
        pr_loop_config: PrLoopConfig | None,
        schedule_config: ScheduleConfig | None = None,
        fleet_config_path: str | None = None,
    ) -> None:
        from agent_fleet.config import load_fleet_config
        from agent_fleet.pr_loop.watcher import PrLoopWatcher
        from agent_fleet.schedule.watcher import ScheduleWatcher

        self.repo = repo
        self.fleet_config = load_fleet_config(fleet_config_path)
        controller_state_root = fleet_state_root(repo)
        targets = iter_target_repos(repo)
        registry = target_registry(targets)

        self.issue_watchers = [
            IssueLoopWatcher(
                target,
                target.issue_dispatch,
                state_root=controller_state_root,
            )
            for target in targets
            if target.issue_dispatch is not None and target.issue_dispatch.enabled
        ]
        if not self.issue_watchers and issue_config and issue_config.enabled:
            self.issue_watchers = [
                IssueLoopWatcher(repo, issue_config, state_root=controller_state_root)
            ]

        self.pr_watchers = [
            PrLoopWatcher(
                target,
                target.pr_loop,
                fleet_config=self.fleet_config,
                state_root=controller_state_root,
            )
            for target in targets
            if target.pr_loop is not None and target.pr_loop.enabled
        ]
        if not self.pr_watchers and pr_loop_config and pr_loop_config.enabled:
            self.pr_watchers = [
                PrLoopWatcher(
                    repo,
                    pr_loop_config,
                    fleet_config=self.fleet_config,
                    state_root=controller_state_root,
                )
            ]

        self.schedule_watcher = (
            ScheduleWatcher(
                repo,
                schedule_config,
                fleet_config_path=fleet_config_path,
                target_registry=registry,
            )
            if schedule_config and schedule_config.enabled
            else None
        )

        # Build backlog dispatchers for targets that have the feature enabled.
        # Each entry is (BacklogDispatcher, tick_interval_s, last_tick_time | None).
        self._backlog_dispatchers: list[tuple[BacklogDispatcher, int, datetime | None]] = []
        # Include the controller repo itself when no explicit targets are configured.
        backlog_targets = targets if targets else [repo]
        for target in backlog_targets:
            bd_cfg = getattr(target, "backlog_dispatcher", None)
            if bd_cfg is None or not bd_cfg.enabled:
                continue
            capacity = target.capacity or FleetCapacity.defaults()
            bd_state_path = state_path(controller_state_root)
            dispatcher = BacklogDispatcher(
                target,
                capacity,
                bd_state_path,
                label=bd_cfg.label,
                persona_label_prefix=bd_cfg.persona_label_prefix,
                default_persona=bd_cfg.default_persona,
                marker_freshness_s=bd_cfg.marker_freshness_s,
            )
            self._backlog_dispatchers.append((dispatcher, bd_cfg.tick_interval_s, None))

        intervals: list[int] = []
        for watcher in self.issue_watchers:
            intervals.append(watcher.config.poll_interval_s)
        for watcher in self.pr_watchers:
            intervals.append(watcher.loop_config.poll_interval_s)
        if schedule_config and schedule_config.enabled:
            intervals.append(schedule_config.poll_interval_s)
        self.poll_interval_s = max(intervals) if intervals else 30

    def poll_once(self) -> dict[str, list[dict[str, str]]]:
        issue_results: list[dict[str, str]] = []
        for watcher in self.issue_watchers:
            issue_results.extend(watcher.poll_once())
        pr_results: list[dict[str, str]] = []
        for watcher in self.pr_watchers:
            pr_results.extend(watcher.poll_once())
        schedule_results = self.schedule_watcher.poll_once() if self.schedule_watcher else []

        # Run backlog dispatchers whose tick interval has elapsed.
        now = datetime.now(UTC)
        updated: list[tuple[BacklogDispatcher, int, datetime | None]] = []
        for dispatcher, tick_interval_s, last_tick in self._backlog_dispatchers:
            elapsed = (
                (now - last_tick).total_seconds() if last_tick is not None else tick_interval_s
            )
            if elapsed >= tick_interval_s:
                try:
                    tick_result = dispatcher.dispatch_once(now)
                    logger.info(
                        "backlog.tick repo=%s considered=%s dispatched=%s skipped=%s",
                        dispatcher.repo.display_name,
                        tick_result.considered,
                        len(tick_result.dispatched),
                        tick_result.skipped_for_reason,
                    )
                    emit_fleet_event(
                        "backlog.tick",
                        repo=dispatcher.repo.display_name,
                        considered=tick_result.considered,
                        dispatched=[(n, p) for n, p in tick_result.dispatched],
                        skipped=tick_result.skipped_for_reason,
                    )
                except Exception as exc:
                    logger.exception("backlog_dispatcher error: %s", exc)
                updated.append((dispatcher, tick_interval_s, now))
            else:
                updated.append((dispatcher, tick_interval_s, last_tick))
        self._backlog_dispatchers = updated

        return {
            "issues": issue_results,
            "prs": pr_results,
            "schedules": schedule_results,
        }

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, _on_sigterm)
        logger.info(
            "Combined fleet watcher started for %s (interval=%ss)",
            self.repo.display_name,
            self.poll_interval_s,
        )
        while not _shutdown_requested:
            try:
                self.poll_once()
            except Exception as exc:
                logger.exception("Combined watcher error: %s", exc)
            if _shutdown_requested:
                break
            time.sleep(self.poll_interval_s)
