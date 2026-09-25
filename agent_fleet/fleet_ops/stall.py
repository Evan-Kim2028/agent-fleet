"""Stall detection — no tool activity for N minutes.

A lane that stops making progress still looks *alive*: the process is running,
so ``lanes status`` reports ``running``, and nothing in the bash drivers
distinguished "thinking hard" from "wedged on a prompt, a rate limit, or a
credential dialog that will never resolve". The lane then burns its whole
budget and produces nothing.

The policy is deliberately conservative: **one** automatic continue, then
escalate. A single continue clears the common causes (a dropped tool result, a
transient provider hiccup, a model that stopped to ask a question nobody will
answer). A second stall is not a transient — it is the operator's problem, and
escalating surfaces it instead of silently spending hours.

Stall is measured from *tool activity*, not from process liveness: the JSONL
stream's mtime and the count of tool events are both signals, and either
settling is a stall.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_STALL_MINUTES = 20

#: The stall action, in order: continue once, then escalate.
ACTION_CONTINUE = "continue"
ACTION_ESCALATE = "escalate"
ACTION_NONE = "none"


@dataclass(frozen=True)
class StallVerdict:
    """What to do about a lane that has gone quiet."""

    action: str
    idle_s: float
    reason: str = ""
    continues_used: int = 0

    @property
    def stalled(self) -> bool:
        return self.action != ACTION_NONE

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "idle_s": self.idle_s,
            "reason": self.reason,
            "continues_used": self.continues_used,
        }


def stream_idle_seconds(
    stream_path: Path | str,
    *,
    now: float | None = None,
    stat: object = None,
) -> float:
    """Seconds since the stream file was last written, or ``inf`` if absent.

    *stat* is an injectable ``os.stat_result`` (tests pass a fake); when omitted
    the file is stat'ed directly.
    """
    if stat is None:
        try:
            mtime = Path(stream_path).stat().st_mtime
        except OSError:
            return float("inf")
    else:
        mtime = stat.st_mtime  # type: ignore[attr-defined]
    return max(0.0, (now if now is not None else time.time()) - mtime)


def judge_stall(
    *,
    idle_s: float,
    stall_minutes: int = DEFAULT_STALL_MINUTES,
    continues_used: int = 0,
    max_continues: int = 1,
    process_alive: bool = True,
) -> StallVerdict:
    """Decide whether a quiet lane should be continued or escalated.

    *idle_s* is how long the lane has shown no tool activity. A lane whose
    process has already exited is not "stalled" — that is a finished or dead
    run, handled by the caller, and mislabelling it would trigger a pointless
    continue against a dead pid.
    """
    if not process_alive:
        return StallVerdict(action=ACTION_NONE, idle_s=idle_s, reason="process is not running")

    threshold_s = max(0.0, float(stall_minutes)) * 60.0
    if idle_s < threshold_s:
        return StallVerdict(action=ACTION_NONE, idle_s=idle_s)

    if continues_used < max_continues:
        return StallVerdict(
            action=ACTION_CONTINUE,
            idle_s=idle_s,
            reason=f"no tool activity for {idle_s / 60:.1f}m (threshold {stall_minutes}m)",
            continues_used=continues_used,
        )

    return StallVerdict(
        action=ACTION_ESCALATE,
        idle_s=idle_s,
        reason=(
            f"no tool activity for {idle_s / 60:.1f}m and the automatic continue "
            f"did not resume progress"
        ),
        continues_used=continues_used,
    )


#: The text sent as the single automatic continue.
CONTINUE_PROMPT = (
    "Continue the task from where you stopped. Do not re-explore what you already read. "
    "Implement the remaining parts, run the targeted tests, commit, push, and open the PR "
    "with gh pr create. Keep responses short and do the work through tool calls."
)


def should_escalate_after_continue(stalled_again: bool, continues_used: int) -> bool:
    """True when a lane that stalled *again* after its one continue must escalate."""
    return bool(stalled_again and continues_used >= 1)
