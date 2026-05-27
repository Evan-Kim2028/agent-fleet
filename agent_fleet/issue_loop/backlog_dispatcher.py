"""Backlog auto-dispatcher: post /agent comments to eligible fleet-ready issues.

Design principle: this module writes NO state of its own. It posts
``/agent --persona <X> <!-- backlog-dispatcher -->`` comments via
``gh issue comment`` and lets the existing watcher comment-trigger path handle
the actual dispatch. This keeps a clean separation and avoids a second writer
on ``.agent-fleet-state.json``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from agent_fleet.capacity import FleetCapacity, FleetCapacityGate, is_visual_audit_dispatch
from agent_fleet.capacity.gate import RETRYABLE_ADMISSION_REASONS
from agent_fleet.in_flight import reap_in_flight
from agent_fleet.integrations.github_cli import gh as _gh
from agent_fleet.issue_loop import github_ops
from agent_fleet.memory import available_ram_gb
from agent_fleet.state import load_state

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.repo import RepoConfig

logger = logging.getLogger(__name__)

BACKLOG_MARKER = "<!-- backlog-dispatcher -->"


@dataclass
class DispatchTickResult:
    """Summary of one backlog dispatcher tick."""

    considered: int = 0
    skipped_for_reason: dict[str, int] = field(default_factory=dict)
    dispatched: list[tuple[int, str]] = field(default_factory=list)

    def _skip(self, reason: str) -> None:
        self.skipped_for_reason[reason] = self.skipped_for_reason.get(reason, 0) + 1


class BacklogDispatcher:
    """Poll GitHub for backlog issues and post /agent comments for eligible ones.

    Constructor args:
        repo: RepoConfig for the target repo.
        capacity: FleetCapacity limits (used for admission check).
        state_path: Path to ``.agent-fleet-state.json`` (read-only access).
        label: GitHub label that marks issues eligible for auto-dispatch.
        persona_label_prefix: Label prefix used to pin a persona to an issue
            (e.g. ``fleet-persona/backend``).
        default_persona: Persona to use when no ``fleet-persona/*`` label found.
        marker_freshness_s: Grace period in seconds — if a
            ``<!-- backlog-dispatcher -->`` comment was posted within this
            window, skip the issue (idempotency guard).
    """

    def __init__(
        self,
        repo: RepoConfig,
        capacity: FleetCapacity,
        state_path: Path,
        *,
        label: str = "fleet-ready",
        persona_label_prefix: str = "fleet-persona/",
        default_persona: str = "data",
        marker_freshness_s: int = 300,
    ) -> None:
        self.repo = repo
        self.capacity_gate = FleetCapacityGate(capacity)
        self._state_path = state_path
        self.label = label
        self.persona_label_prefix = persona_label_prefix
        self.default_persona = default_persona
        self.marker_freshness_s = marker_freshness_s

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _list_label_issues(self) -> list[dict[str, Any]]:
        """Return open issues carrying ``self.label`` via gh CLI."""
        result = _gh(
            "issue",
            "list",
            "--label",
            self.label,
            "--state",
            "open",
            "--json",
            "number,labels,comments",
            "--limit",
            "100",
            cwd=self.repo.repo_root,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            logger.debug(
                "backlog_dispatcher: gh issue list failed rc=%s stderr=%s",
                result.returncode,
                result.stderr.strip(),
            )
            return []
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            logger.debug("backlog_dispatcher: invalid JSON from gh issue list")
            return []

    def _issue_has_mutex_label(self, labels: list[str], issue_number: int) -> bool:
        """Return True if the issue carries an ``agent-running/<N>`` mutex label."""
        mutex_label = f"agent-running/{issue_number}"
        return mutex_label in labels

    def _has_recent_marker(self, comments: list[dict[str, Any]], now: datetime) -> bool:
        """Return True if a backlog-dispatcher marker comment exists within freshness window."""
        for comment in comments:
            body = str(comment.get("body") or "")
            if BACKLOG_MARKER not in body:
                continue
            created_at_raw = str(comment.get("createdAt") or "")
            if not created_at_raw:
                # Treat unknown timestamps as fresh to be safe.
                return True
            try:
                created_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
                age_s = (now - created_at).total_seconds()
                if age_s < self.marker_freshness_s:
                    return True
            except ValueError:
                logger.debug(
                    "backlog_dispatcher: could not parse comment timestamp %r",
                    created_at_raw,
                )
                return True  # Be conservative: treat as fresh.
        return False

    def _pick_persona(self, labels: list[str]) -> str:
        """Return the first ``fleet-persona/<X>`` label's X, else default_persona."""
        for label in labels:
            if label.startswith(self.persona_label_prefix):
                persona = label[len(self.persona_label_prefix) :]
                if persona:
                    return persona
        return self.default_persona

    def _post_dispatch_comment(self, issue_number: int, persona: str) -> None:
        body = f"/agent --persona {persona} {BACKLOG_MARKER}"
        github_ops.post_issue_comment(issue_number, body, cwd=self.repo.repo_root)
        logger.info(
            "backlog_dispatcher: posted /agent comment on #%s persona=%s",
            issue_number,
            persona,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def dispatch_once(self, now: datetime) -> DispatchTickResult:
        """Evaluate backlog issues and post /agent comments to eligible ones.

        This method is idempotent: issues that already have a recent marker
        comment are skipped. It reads the state file for in_flight data but
        does not write to it.

        Args:
            now: Current time (passed explicitly for testability).

        Returns:
            DispatchTickResult summarising what was considered, skipped, and
            dispatched.
        """
        result = DispatchTickResult()

        # Load state read-only for in_flight membership check.
        state: dict[str, Any] = {}
        try:
            state = load_state(self._state_path)
        except Exception as exc:
            logger.warning("backlog_dispatcher: could not read state file: %s", exc)
        # Reap dead PIDs so the in_flight set is accurate.
        reap_in_flight(state)

        # Fetch all open issues with the backlog label.
        issues = self._list_label_issues()
        if not issues:
            return result

        # Cache open-PR issue numbers (one gh call for all issues).
        try:
            open_pr_issues = github_ops.open_fleet_pr_issue_numbers(
                branch_prefixes=("agent-fleet/", "fleet/"),
                cwd=self.repo.repo_root,
            )
        except Exception as exc:
            logger.warning("backlog_dispatcher: could not fetch open PRs: %s", exc)
            open_pr_issues = set()

        ram_gb = available_ram_gb()
        capacity_limit = self.capacity_gate.capacity.max_dispatches
        in_flight_base = len(list((state.get("in_flight") or {}).values()))

        for issue in issues:
            # Stop as soon as this tick has filled the remaining capacity.
            if len(result.dispatched) >= capacity_limit - in_flight_base:
                break

            issue_number = int(issue.get("number", 0))
            if issue_number <= 0:
                continue

            result.considered += 1

            # --- Cheap filter 1: in_flight check ---
            in_flight_map: dict[str, Any] = state.get("in_flight") or {}
            if str(issue_number) in in_flight_map:
                result._skip("in_flight")
                logger.debug("backlog_dispatcher: skip #%s — in_flight", issue_number)
                continue

            # --- Cheap filter 2: open PR ---
            if issue_number in open_pr_issues:
                result._skip("open_pr")
                logger.debug("backlog_dispatcher: skip #%s — open PR exists", issue_number)
                continue

            # --- Cheap filter 3: mutex label ---
            raw_labels = issue.get("labels") or []
            labels: list[str] = []
            for lbl in raw_labels:
                if isinstance(lbl, dict):
                    labels.append(str(lbl.get("name") or ""))
                else:
                    labels.append(str(lbl))
            labels = [lbl for lbl in labels if lbl]

            if self._issue_has_mutex_label(labels, issue_number):
                result._skip("mutex_label")
                logger.debug("backlog_dispatcher: skip #%s — mutex label", issue_number)
                continue

            # --- Cheap filter 4: recent backlog-dispatcher marker ---
            raw_comments = issue.get("comments") or []
            comments: list[dict[str, Any]] = [c for c in raw_comments if isinstance(c, dict)]
            if self._has_recent_marker(comments, now):
                result._skip("recent_marker")
                logger.debug("backlog_dispatcher: skip #%s — recent marker comment", issue_number)
                continue

            # --- Persona selection ---
            persona = self._pick_persona(labels)

            # --- Visual-audit classification (mirrors watcher.py) ---
            is_visual_audit = is_visual_audit_dispatch(issue_labels=labels)

            # --- Capacity check ---
            admission = self.capacity_gate.try_admit(
                state,
                issue_number=issue_number,
                persona=persona,
                is_visual_audit=is_visual_audit,
                available_ram_gb=ram_gb,
            )
            if not admission.allowed:
                result._skip(f"capacity:{admission.reason}")
                logger.info(
                    "backlog_dispatcher: capacity gate refused #%s persona=%s reason=%s",
                    issue_number,
                    persona,
                    admission.reason,
                )
                if admission.reason in RETRYABLE_ADMISSION_REASONS:
                    break
                continue

            # --- Post the /agent comment ---
            try:
                self._post_dispatch_comment(issue_number, persona)
            except Exception as exc:
                logger.warning(
                    "backlog_dispatcher: failed to post comment on #%s: %s",
                    issue_number,
                    exc,
                )
                result._skip("comment_failed")
                continue

            result.dispatched.append((issue_number, persona))

        return result
