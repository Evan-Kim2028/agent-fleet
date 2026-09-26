"""Escalation routing: what happens to an item that needs a decision.

The gate and the lane runner both end items in the same place — a
``NEEDS-ESCALATION`` line — for four quite different reasons. Routing them all
one way is what makes an operator's queue unusable: a batch of "decisions
needed" that is mostly "pytest could not start, try again" trains people to
ignore the queue.

So the reason is classified, and the class decides the action:

``infra``
    Something in the environment failed and the work itself is fine — a test
    runner could not start, a worktree could not be created, a git fetch timed
    out. **Retried once automatically.** Not more: an infra failure that
    survives one retry is an infra failure that needs a human, and a retry loop
    on a broken machine just multiplies the damage.

``untestable``
    A real defect that no test can express — a docs contradiction, a script's
    observed behaviour. **Routed to a fix round**, because the fix is ordinary
    work with an unusual shape, not a decision.

``fence`` / ``owner_decision``
    A human deliberately fenced this, or the answer is a judgement call about
    intent. **Queued for a human, in batches.** Never retried automatically.
    This is the class where a wrong automatic action is most expensive: it
    means re-running something a human fenced on purpose.

Anything unrecognised goes to the human queue. That default is the whole point.
An unparsed reason is a reason this code has never seen, and guessing
``infra`` for it would mean an automatic retry of a fence violation by anyone
whose gate grew a new reason string. When in doubt, ask a human.

The human queue is a file, not a ping. Per-item notification is how a fleet of
this size becomes unmonitorable — a hundred notifications train a human to mute
the channel, and then the one that mattered goes unread.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agent_fleet.serve.events import emit_serve_event
from agent_fleet.serve.paths import decisions_path

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.serve.clock import Clock

CLASS_INFRA = "infra"
CLASS_UNTESTABLE = "untestable"
CLASS_FENCE = "fence"
CLASS_OWNER = "owner_decision"
CLASS_UNKNOWN = "unknown"

#: Every class routing knows about.
REASON_CLASSES = (CLASS_INFRA, CLASS_UNTESTABLE, CLASS_FENCE, CLASS_OWNER, CLASS_UNKNOWN)

#: What each class does. ``retry`` is capped at one attempt by
#: :data:`MAX_INFRA_RETRIES`; ``decide`` always means a human looks at it.
ACTION_RETRY = "retry"
ACTION_FIX_ROUND = "fix_round"
ACTION_DECIDE = "decide"

#: How many times an infra escalation may be retried automatically. One: a
#: second failure of the same kind means the machine is broken, and the remedy
#: is a human, not another attempt.
MAX_INFRA_RETRIES = 1

#: Markers that identify a class, checked in order. The order is the
#: precedence: "fence" wins over a generic "infra" word because a fenced item
#: that also mentions a test run is still a fenced item.
_CLASS_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        CLASS_FENCE,
        (
            "fence",
            "fenced",
            "standing fence",
            "owner fence",
            "forbidden",
            "do not edit",
        ),
    ),
    (
        CLASS_OWNER,
        (
            "owner decision",
            "owner call",
            "needs a decision",
            "product decision",
            "ambiguous",
            "clarification",
        ),
    ),
    (
        CLASS_UNTESTABLE,
        (
            "untestable",
            "cannot be expressed as a test",
            "no test can",
            "judge-confirmed",
            "docs contradiction",
        ),
    ),
    (
        CLASS_INFRA,
        (
            "infra",
            "could not run",
            "cannot run",
            "worktree",
            "git fetch",
            "network",
            "timeout",
            "timed out",
            "no space",
            "oom",
            "dependency",
        ),
    ),
)

#: An explicit class token in the reason wins over marker matching, so a
#: component that knows exactly what it is complaining about does not have to
#: rely on prose. ``class=infra`` or ``[class: fence]``.
_CLASS_TOKEN = re.compile(r"(?:class=|\[class:\s*)([a-z_]+)\s*\]?", re.IGNORECASE)


def classify_reason(reason: str) -> str:
    """Map an escalation reason onto a :data:`REASON_CLASSES` member.

    An explicit ``class=`` token is authoritative. Otherwise markers are
    matched longest-first within each class, and the first class with a hit
    wins. No match is :data:`CLASS_UNKNOWN`, which routes to a human.
    """
    text = (reason or "").strip()
    if not text:
        return CLASS_UNKNOWN

    explicit = _CLASS_TOKEN.search(text)
    if explicit:
        token = explicit.group(1).lower()
        if token in REASON_CLASSES:
            return token
        if token == "owner":
            return CLASS_OWNER
        if token == "testable":
            return CLASS_UNTESTABLE

    lowered = text.lower()
    for reason_class, markers in _CLASS_MARKERS:
        if any(marker in lowered for marker in markers):
            return reason_class
    return CLASS_UNKNOWN


@dataclass(frozen=True)
class Decision:
    """One item parked for a human, with enough context to decide in a batch."""

    item_id: str
    reason: str
    reason_class: str
    raised_epoch: float
    repo: str = ""
    pr: int | None = None
    head: str | None = None
    stage: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "item_id": self.item_id,
            "reason": self.reason,
            "reason_class": self.reason_class,
            "raised_epoch": self.raised_epoch,
        }
        for key, value in (
            ("repo", self.repo),
            ("pr", self.pr),
            ("head", self.head),
            ("stage", self.stage),
        ):
            if value not in (None, ""):
                payload[key] = value
        if self.detail:
            payload["detail"] = dict(self.detail)
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Decision | None:
        item_id = raw.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            return None
        detail = raw.get("detail")
        epoch = raw.get("raised_epoch")
        pr = raw.get("pr")
        return cls(
            item_id=item_id,
            reason=str(raw.get("reason") or ""),
            reason_class=str(raw.get("reason_class") or CLASS_UNKNOWN),
            raised_epoch=float(epoch) if isinstance(epoch, int | float) else 0.0,
            repo=str(raw.get("repo") or ""),
            pr=int(pr) if isinstance(pr, int) else None,
            head=str(raw["head"]) if raw.get("head") else None,
            stage=str(raw.get("stage") or ""),
            detail=dict(detail) if isinstance(detail, dict) else {},
        )


@dataclass(frozen=True)
class Route:
    """The decision made for one escalation."""

    item_id: str
    reason_class: str
    action: str
    reason: str = ""
    attempt: int = 1
    queued: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "reason_class": self.reason_class,
            "action": self.action,
            "reason": self.reason,
            "attempt": self.attempt,
            "queued": self.queued,
        }


def action_for(reason_class: str, *, attempts: int = 0) -> str:
    """The action a class maps to. Pure, so the routing table is testable."""
    if reason_class == CLASS_INFRA:
        return ACTION_RETRY if attempts < MAX_INFRA_RETRIES else ACTION_DECIDE
    if reason_class == CLASS_UNTESTABLE:
        return ACTION_FIX_ROUND
    return ACTION_DECIDE


def read_decisions(path: Path) -> list[Decision]:
    """Every queued decision, oldest first."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[Decision] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, dict):
            decision = Decision.from_dict(raw)
            if decision is not None:
                out.append(decision)
    return out


class EscalationRouter:
    """Classifies an escalation, routes it, and keeps the human queue."""

    def __init__(
        self,
        operator: str,
        *,
        clock: Clock,
        decisions_file: Path | None = None,
    ) -> None:
        self.operator = operator
        self.clock = clock
        self._decisions_file = decisions_file
        #: item -> how many automatic retries it has already had.
        self.attempts: dict[str, int] = {}

    @property
    def decisions_path(self) -> Path:
        return self._decisions_file or decisions_path(self.operator)

    def attempts_for(self, item_id: str) -> int:
        return self.attempts.get(item_id, 0)

    def route(
        self,
        item_id: str,
        reason: str,
        *,
        repo: str = "",
        pr: int | None = None,
        head: str | None = None,
        stage: str = "",
        detail: dict[str, Any] | None = None,
    ) -> Route:
        """Classify, act, and record. Returns the :class:`Route` taken."""
        reason_class = classify_reason(reason)
        attempts = self.attempts_for(item_id)
        action = action_for(reason_class, attempts=attempts)

        if action == ACTION_RETRY:
            self.attempts[item_id] = attempts + 1
        elif action == ACTION_DECIDE and reason_class == CLASS_INFRA:
            # The one automatic retry was already spent; stop trying.
            self.attempts[item_id] = attempts + 1

        queued = action == ACTION_DECIDE
        if queued:
            self.raise_decision(
                item_id,
                reason,
                reason_class=reason_class,
                repo=repo,
                pr=pr,
                head=head,
                stage=stage,
                detail=detail,
            )

        route = Route(
            item_id=item_id,
            reason_class=reason_class,
            action=action,
            reason=reason,
            attempt=attempts + 1,
            queued=queued,
        )
        emit_serve_event(
            self.operator,
            "serve.escalation.routed",
            level="warning" if queued else "info",
            data=route.to_dict(),
        )
        return route

    def raise_decision(
        self,
        item_id: str,
        reason: str,
        *,
        reason_class: str = CLASS_UNKNOWN,
        repo: str = "",
        pr: int | None = None,
        head: str | None = None,
        stage: str = "",
        detail: dict[str, Any] | None = None,
    ) -> Decision:
        """Append to the human queue. One line, append-only, never rewritten.

        A human resolves a decision by appending a resolution record, so the
        queue is a log rather than a mutable list — a human working through
        twenty of these and a supervisor appending the twenty-first must not be
        able to lose each other's work.
        """
        decision = Decision(
            item_id=item_id,
            reason=reason,
            reason_class=reason_class,
            raised_epoch=self.clock.time(),
            repo=repo,
            pr=pr,
            head=head,
            stage=stage,
            detail=detail or {},
        )
        path = self.decisions_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(decision.to_dict(), default=str) + "\n")
        return decision

    def pending(self) -> list[Decision]:
        """Open decisions — raised, and not resolved since they were raised.

        Resolutions are applied *after* the whole log is read rather than as
        the lines are scanned. A single forward pass gets this wrong in a way
        that matters: a decision raised after a resolution for the same item
        would look open, and one raised before it would look resolved, even
        though the item is plainly still waiting. A human who resolved a fence,
        watched the lane come back, watched it fence again, and then checked
        the queue would be told their answer had already been given.
        """
        try:
            text = self.decisions_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        open_by_id: dict[str, Decision] = {}
        order: list[str] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            item_id = raw.get("item_id")
            if not isinstance(item_id, str) or not item_id:
                continue
            if raw.get("resolved"):
                if open_by_id.pop(item_id, None) is not None and item_id in order:
                    order.remove(item_id)
                continue
            decision = Decision.from_dict(raw)
            if decision is None:
                continue
            if item_id not in open_by_id:
                order.append(item_id)
            open_by_id[item_id] = decision
        return [open_by_id[item_id] for item_id in order if item_id in open_by_id]

    def resolve(self, item_id: str, resolution: str) -> bool:
        """Append a resolution, closing the decision. Returns False if unknown."""
        if not any(d.item_id == item_id for d in self.pending()):
            return False
        self.attempts.pop(item_id, None)
        path = self.decisions_path
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "item_id": item_id,
            "resolved": True,
            "resolution": resolution,
            "resolved_epoch": self.clock.time(),
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
        emit_serve_event(
            self.operator,
            "serve.decision.resolved",
            data={"item_id": item_id, "resolution": resolution},
        )
        return True


__all__ = [
    "ACTION_DECIDE",
    "ACTION_FIX_ROUND",
    "ACTION_RETRY",
    "CLASS_FENCE",
    "CLASS_INFRA",
    "CLASS_OWNER",
    "CLASS_UNKNOWN",
    "CLASS_UNTESTABLE",
    "MAX_INFRA_RETRIES",
    "REASON_CLASSES",
    "Decision",
    "EscalationRouter",
    "Route",
    "action_for",
    "classify_reason",
    "read_decisions",
]
