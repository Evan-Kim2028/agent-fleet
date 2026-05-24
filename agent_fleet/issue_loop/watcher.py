"""Poll GitHub issue comments for /agent --persona triggers."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_fleet.capacity import (
    RETRYABLE_ADMISSION_REASONS,
    FleetCapacity,
    FleetCapacityGate,
    count_visual_in_flight,
    is_visual_audit_dispatch,
)
from agent_fleet.issue_loop import github_ops
from agent_fleet.issue_loop.state import load_state, now_iso, save_state, state_path
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
from agent_fleet.observability.events import FleetEvent
from agent_fleet.observability.sinks import PythonLoggingSink
from agent_fleet.repo import RepoConfig, find_repo_config

if TYPE_CHECKING:
    from agent_fleet.issue_loop.config import IssueDispatchConfig
    from agent_fleet.pr_loop.config import PrLoopConfig

logger = logging.getLogger(__name__)

_watcher_event_sink = PythonLoggingSink("agent_fleet.watcher")

_shutdown_requested = False


def _on_sigterm(_signum: int, _frame: object) -> None:
    global _shutdown_requested
    _shutdown_requested = True


def _dispatch_executable() -> list[str]:
    return [sys.executable, "-m", "agent_fleet.issue_loop.dispatch"]


def _pid_is_dispatch(pid: int) -> bool:
    try:
        with Path(f"/proc/{pid}/cmdline").open("rb") as handle:
            return b"agent_fleet.issue_loop.dispatch" in handle.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return False


def _reap_in_flight(state: dict[str, Any]) -> None:
    in_flight = state.setdefault("in_flight", {})
    for issue_key, runs in list(in_flight.items()):
        alive = [run for run in runs if _pid_is_dispatch(int(run["pid"]))]
        if alive:
            in_flight[issue_key] = alive
        else:
            in_flight.pop(issue_key, None)


def _cleanup_orphaned_playwright_mcp(state: dict[str, Any]) -> None:
    """Force-kill lingering Playwright MCP processes when no visual audits are active."""
    if count_visual_in_flight(state) > 0:
        return
    before = count_playwright_mcp_processes()
    if before == 0:
        return
    logger.warning("Orphaned playwright MCP processes detected: count=%s", before)
    result = cleanup_playwright_mcp_processes(force_kill=True)
    _watcher_event_sink.emit(
        FleetEvent.now(
            run_id="watcher",
            event="mcp.orphan.cleanup",
            level="warning" if result.after > 0 else "info",
            data={
                "before": result.before,
                "after": result.after,
                "force_killed": list(result.force_killed),
                "cleaned": result.cleaned,
            },
        )
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
) -> int | None:
    env = os.environ.copy()
    env["ISSUE_NUMBER"] = str(issue_number)
    env["COMMENT_BODY"] = comment_body
    env["PERSONA"] = persona
    env["AGENT_FLEET_WORKSPACE"] = str(repo_root)
    proc = subprocess.Popen(
        _dispatch_executable(),
        env=env,
        cwd=str(repo_root),
        start_new_session=True,
    )
    return proc.pid


class IssueLoopWatcher:
    """Long-running watcher for /agent issue comment triggers."""

    def __init__(self, repo: RepoConfig, dispatch_config: IssueDispatchConfig) -> None:
        self.repo = repo
        self.config = dispatch_config
        self.state_file = state_path(repo.repo_root, dispatch_config.state_file)
        capacity = repo.capacity or FleetCapacity.defaults()
        self.capacity_gate = FleetCapacityGate(capacity)

    def poll_once(self, state: dict[str, Any] | None = None) -> list[dict[str, str]]:
        if state is None:
            state = load_state(self.state_file)
        _reap_in_flight(state)
        _cleanup_orphaned_playwright_mcp(state)
        results: list[dict[str, str]] = []

        repo_name = github_ops.repo_full_name(cwd=self.repo.repo_root)
        comments = github_ops.poll_issue_comments(
            repo_name,
            state["since"],
            cwd=self.repo.repo_root,
        )
        new_since = now_iso()

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
                _watcher_event_sink.emit(
                    FleetEvent.now(
                        run_id=f"issue-{issue_number}",
                        event="admission.check",
                        level="warning" if retryable else "info",
                        issue_number=issue_number,
                        persona=persona,
                        data={
                            "allowed": False,
                            "reason": admission.reason,
                            "retryable": retryable,
                            "visual_audit": is_visual_audit,
                        },
                    )
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
            pid = _spawn_dispatch(
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

        state["since"] = new_since
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
    """Issue dispatch + PR loop in one poll cycle."""

    def __init__(
        self,
        repo: RepoConfig,
        *,
        issue_config: IssueDispatchConfig,
        pr_loop_config: PrLoopConfig | None,
        fleet_config_path: str | None = None,
    ) -> None:
        from agent_fleet.config import load_fleet_config
        from agent_fleet.pr_loop.watcher import PrLoopWatcher

        self.repo = repo
        self.issue_watcher = IssueLoopWatcher(repo, issue_config)
        self.fleet_config = load_fleet_config(fleet_config_path)
        self.pr_watcher = (
            PrLoopWatcher(repo, pr_loop_config, fleet_config=self.fleet_config)
            if pr_loop_config and pr_loop_config.enabled
            else None
        )
        self.poll_interval_s = max(
            issue_config.poll_interval_s,
            pr_loop_config.poll_interval_s if pr_loop_config else issue_config.poll_interval_s,
        )

    def poll_once(self) -> dict[str, list[dict[str, str]]]:
        issue_results = self.issue_watcher.poll_once()
        pr_results = self.pr_watcher.poll_once() if self.pr_watcher else []
        return {"issues": issue_results, "prs": pr_results}

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
