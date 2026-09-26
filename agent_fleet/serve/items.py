"""The item board — one record per lane, one line per transition.

``serve status`` has to answer "what is the fleet doing, what is stuck, and how
fast is it shipping". None of that is in any one component: the dispatcher
knows what it launched, the gate knows its verdict, the merge executor knows
what it merged. So serve keeps its own append-only board where each component
reports stage transitions, and derives depth, throughput and "why is this
waiting" from it.

The board is **append-only on purpose**. A mutable current-state file looks
simpler but throws away exactly the history the throughput numbers need, and
loses everything on a crash between two writes. One JSON object per line means
a crash truncates at most the line being written, and the reader skips it.

Stages, in pipeline order::

    queued -> running -> gating -> approved -> merging -> merged
                        \\-> escalated

``escalated`` is terminal but not dead: the routing in
:mod:`agent_fleet.serve.escalate` may move an item back to ``queued`` for an
infra retry or a fix round, and those transitions are recorded like any other
so the throughput numbers count real work, not retries as if they were new work.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agent_fleet.serve.clock import SystemClock

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.serve.capacity import CapacityTargets
    from agent_fleet.serve.clock import Clock

STAGE_QUEUED = "queued"
STAGE_RUNNING = "running"
STAGE_GATING = "gating"
STAGE_APPROVED = "approved"
STAGE_MERGING = "merging"
STAGE_MERGED = "merged"
STAGE_ESCALATED = "escalated"

#: Display and depth order. Terminal stages come last so a status screen reads
#: as a pipeline rather than an alphabetical accident.
STAGES: tuple[str, ...] = (
    STAGE_QUEUED,
    STAGE_RUNNING,
    STAGE_GATING,
    STAGE_APPROVED,
    STAGE_MERGING,
    STAGE_MERGED,
    STAGE_ESCALATED,
)

#: Stages from which no further work happens on its own.
TERMINAL_STAGES = frozenset({STAGE_MERGED, STAGE_ESCALATED})

#: Stages a watchdog stage timeout applies to, and the config key for each.
STAGE_OF_ITEM = "lane"


@dataclass(frozen=True)
class Item:
    """An item's latest state, folded from its transitions."""

    item_id: str
    repo: str = ""
    stage: str = STAGE_QUEUED
    entered_epoch: float = 0.0
    first_seen_epoch: float = 0.0
    pr: int | None = None
    head: str | None = None
    #: Why this item is not moving, in the component's own words.
    wait_reason: str = ""
    #: Set when escalated: the reason class that routed it there.
    reason_class: str = ""
    #: Free-form component tags (cluster, depends_on, engine).
    tags: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.stage in TERMINAL_STAGES

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "repo": self.repo,
            "stage": self.stage,
            "entered_epoch": self.entered_epoch,
            "first_seen_epoch": self.first_seen_epoch,
            "pr": self.pr,
            "head": self.head,
            "wait_reason": self.wait_reason,
            "reason_class": self.reason_class,
            "tags": dict(self.tags),
        }


@dataclass(frozen=True)
class Transition:
    """One append to the board. Never rewritten."""

    item_id: str
    stage: str
    epoch: float
    repo: str = ""
    pr: int | None = None
    head: str | None = None
    wait_reason: str = ""
    reason_class: str = ""
    component: str = ""
    tags: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "item_id": self.item_id,
            "stage": self.stage,
            "epoch": self.epoch,
        }
        for key, value in (
            ("repo", self.repo),
            ("pr", self.pr),
            ("head", self.head),
            ("wait_reason", self.wait_reason),
            ("reason_class", self.reason_class),
            ("component", self.component),
        ):
            if value not in (None, ""):
                payload[key] = value
        if self.tags:
            payload["tags"] = dict(self.tags)
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Transition | None:
        item_id = raw.get("item_id")
        stage = raw.get("stage")
        if not isinstance(item_id, str) or not isinstance(stage, str) or stage not in STAGES:
            return None
        epoch = raw.get("epoch")
        tags = raw.get("tags")
        return cls(
            item_id=item_id,
            stage=stage,
            epoch=float(epoch) if isinstance(epoch, int | float) else 0.0,
            repo=str(raw.get("repo") or ""),
            pr=int(raw["pr"]) if isinstance(raw.get("pr"), int) else None,
            head=str(raw["head"]) if raw.get("head") else None,
            wait_reason=str(raw.get("wait_reason") or ""),
            reason_class=str(raw.get("reason_class") or ""),
            component=str(raw.get("component") or ""),
            tags=dict(tags) if isinstance(tags, dict) else {},
        )


def read_transitions(path: Path) -> list[Transition]:
    """Parse the board, skipping any line a crash left half-written."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[Transition] = []
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
        transition = Transition.from_dict(raw)
        if transition is not None:
            out.append(transition)
    return out


def fold(items: list[Transition]) -> dict[str, Item]:
    """Collapse a transition log into the latest state per item."""
    folded: dict[str, Item] = {}
    for transition in items:
        previous = folded.get(transition.item_id)
        folded[transition.item_id] = Item(
            item_id=transition.item_id,
            repo=transition.repo or (previous.repo if previous else ""),
            stage=transition.stage,
            entered_epoch=transition.epoch,
            first_seen_epoch=previous.first_seen_epoch if previous else transition.epoch,
            pr=transition.pr if transition.pr is not None else (previous.pr if previous else None),
            head=transition.head or (previous.head if previous else None),
            wait_reason=transition.wait_reason or (previous.wait_reason if previous else ""),
            reason_class=transition.reason_class or (previous.reason_class if previous else ""),
            tags=transition.tags or (previous.tags if previous else {}),
        )
    return folded


def _wait_reason(
    item: Item,
    *,
    now: float,
    targets: CapacityTargets | None,
) -> str:
    """Why this item is sitting where it is.

    The component's own words win — a lane that says "blocked on depends_on
    foo" knows something the controller does not. The controller's arithmetic is
    only the fallback for "queued with no stated reason", which is exactly the
    case where a human needs an answer.
    """
    if item.wait_reason:
        return item.wait_reason
    age_min = max(0.0, now - item.entered_epoch) / 60.0
    if item.stage == STAGE_QUEUED:
        if targets is not None and targets.gates_priority:
            return f"gates_priority: holding new lanes at floor (queued {age_min:.0f}m)"
        if targets is not None and item.entered_epoch >= 0 and targets.max_lanes <= 1:
            return f"lane floor is 1 under pressure (queued {age_min:.0f}m)"
        return f"no lane slot reported free (queued {age_min:.0f}m)"
    if item.stage == STAGE_GATING:
        cap = f"max_gates={targets.max_gates}" if targets else "gate slots untracked"
        return f"waiting for a gate slot ({cap}, gating {age_min:.0f}m)"
    if item.stage == STAGE_APPROVED:
        return f"approved, waiting for the merge executor ({age_min:.0f}m)"
    if item.stage == STAGE_MERGING:
        return f"merging ({age_min:.0f}m)"
    if item.stage == STAGE_ESCALATED:
        return f"escalated as {item.reason_class or 'unspecified'}"
    return f"in {item.stage} for {age_min:.0f}m"


@dataclass(frozen=True)
class StageStats:
    """One row of the status screen."""

    stage: str
    depth: int
    oldest: Item | None
    oldest_age_s: float
    wait_reason: str
    per_hour: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "depth": self.depth,
            "oldest_item": self.oldest.item_id if self.oldest else None,
            "oldest_age_s": round(self.oldest_age_s, 1),
            "wait_reason": self.wait_reason,
            "per_hour": round(self.per_hour, 2),
        }


class ItemBoard:
    """Reads and appends the transition log; derives everything the status shows."""

    def __init__(self, path: Path, *, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def transitions(self) -> list[Transition]:
        return read_transitions(self.path)

    def items(self) -> dict[str, Item]:
        return fold(self.transitions())

    def append(self, transition: Transition) -> None:
        """One line, appended, fsync'd.

        The fsync matters more here than anywhere else in serve: the board is
        written by components that may be killed by the watchdog moments later,
        and a stage transition that never reached disk is a lane the supervisor
        believes is still queued. ``O_APPEND`` keeps concurrent appenders from
        interleaving, and the write is short enough to be atomic.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(transition.to_dict(), default=str) + "\n"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()

    def record(
        self,
        item_id: str,
        stage: str,
        *,
        repo: str = "",
        pr: int | None = None,
        head: str | None = None,
        wait_reason: str = "",
        reason_class: str = "",
        component: str = "",
        tags: dict[str, Any] | None = None,
    ) -> Transition | None:
        """Append a transition, rejecting an unknown stage.

        Returns ``None`` for an unknown stage rather than writing it: a typo in
        a stage name would otherwise create a stage that no depth count, no
        throughput number and no status row knows about, and the item would
        silently vanish from the board.
        """
        if stage not in STAGES:
            return None
        transition = Transition(
            item_id=item_id,
            stage=stage,
            epoch=self.clock.time(),
            repo=repo,
            pr=pr,
            head=head,
            wait_reason=wait_reason,
            reason_class=reason_class,
            component=component,
            tags=tags or {},
        )
        self.append(transition)
        return transition

    def depth(self, items: dict[str, Item] | None = None) -> dict[str, int]:
        current = items if items is not None else self.items()
        counts = dict.fromkeys(STAGES, 0)
        for item in current.values():
            counts[item.stage] = counts.get(item.stage, 0) + 1
        return counts

    def throughput(
        self, *, window_hours: float = 1.0, now: float | None = None
    ) -> dict[str, float]:
        """Entries into each stage per hour over the trailing window.

        Counted from transitions, not from the current snapshot, so a queue that
        drained through six stages in the window shows as six stage entries
        rather than one.
        """
        the_now = now if now is not None else self.clock.time()
        since = the_now - window_hours * 3600.0
        counts = dict.fromkeys(STAGES, 0)
        for transition in self.transitions():
            if transition.epoch >= since:
                counts[transition.stage] = counts.get(transition.stage, 0) + 1
        if window_hours <= 0:
            return dict.fromkeys(STAGES, 0.0)
        return {stage: count / window_hours for stage, count in counts.items()}

    def stage_stats(
        self,
        *,
        targets: CapacityTargets | None = None,
        window_hours: float = 1.0,
        now: float | None = None,
    ) -> list[StageStats]:
        """One :class:`StageStats` per stage, in pipeline order."""
        the_now = now if now is not None else self.clock.time()
        current = self.items()
        per_hour = self.throughput(window_hours=window_hours, now=the_now)
        out: list[StageStats] = []
        for stage in STAGES:
            in_stage = [i for i in current.values() if i.stage == stage]
            oldest = min(in_stage, key=lambda i: i.entered_epoch, default=None)
            age = (the_now - oldest.entered_epoch) if oldest else 0.0
            out.append(
                StageStats(
                    stage=stage,
                    depth=len(in_stage),
                    oldest=oldest,
                    oldest_age_s=max(0.0, age),
                    wait_reason=(
                        _wait_reason(oldest, now=the_now, targets=targets) if oldest else ""
                    ),
                    per_hour=per_hour.get(stage, 0.0),
                )
            )
        return out

    def completed_this_tick(self, *, now: float | None = None) -> int:
        """How many items reached a terminal stage since the last tick.

        This is the completion signal the starvation guard consumes. It counts
        *any* terminal transition rather than only ``merged``, because a run
        that ships nothing but escalations is still failing to ship and should
        not be mistaken for progress.
        """
        the_now = now if now is not None else self.clock.time()
        return sum(
            1 for t in self.transitions() if t.stage in TERMINAL_STAGES and t.epoch <= the_now
        )


def stage_from_status_line(line: str) -> str | None:
    """Classify a component status line into a stage.

    The bash drivers communicate by appending free text to ``lanes/<lane>.status``
    and grepping it (``PREMERGE-APPROVED``, ``NEEDS-ESCALATION``, ``start @``).
    Rather than require every component to be rewritten before the board has
    data, the watchdog and janitor feed those lines through here so serve can
    report on a fleet that is still running the old drivers.
    """
    text = line.strip()
    if not text:
        return None
    if "PREMERGE-APPROVED" in text or "loop result: APPROVE" in text:
        return STAGE_APPROVED
    if "NEEDS-ESCALATION" in text or "NEEDS-REBASE" in text:
        return STAGE_ESCALATED
    if "gate:" in text or "start @" in text:
        return STAGE_GATING
    if "merged" in text.lower() or "MERGED" in text:
        return STAGE_MERGING
    return STAGE_RUNNING


__all__ = [
    "STAGES",
    "STAGE_APPROVED",
    "STAGE_ESCALATED",
    "STAGE_GATING",
    "STAGE_MERGED",
    "STAGE_MERGING",
    "STAGE_QUEUED",
    "STAGE_RUNNING",
    "TERMINAL_STAGES",
    "Item",
    "ItemBoard",
    "StageStats",
    "Transition",
    "fold",
    "read_transitions",
    "stage_from_status_line",
]
