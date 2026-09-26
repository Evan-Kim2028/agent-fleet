"""Attempt counters for the routing policy.

The reconciler kept these as two append-only text files under
``$FLEET_OPS_HOME``, one ``<lane> <sha9>`` record per line. That is kept here
rather than moved to a database: the files are read by ``pr_triage.py`` and
written by the reconciler on the same machine, so a new format would need a
migration of live state and would break the other reader for no gain.

Keyed on ``(lane, head)``, not on the PR, because a new push resets the budget —
the whole point of the cap is "three tries at *this* code", and carrying the
count across a push would make an improved head look like a spent one.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from agent_fleet.routing.policy import RouteCounters

#: Default ``$FLEET_OPS_HOME``, matching the bash drivers' fallback.
DEFAULT_OPS_HOME = Path("~/fleet/ops")

#: Re-gate attempts, ``<lane> <sha9>`` per line. The 3-per-head cap.
REGATE_LOG = "requeued_failclosed.txt"
#: No-push / untestable rewrites, ``<lane> <sha9>`` per line. Once per head.
REWORK_LOG = "reworked.txt"
#: Rebase attempts, ``<lane> <sha9>`` per line. Once per head.
REBASE_LOG = "rebased.txt"
#: Repair attempts, ``<lane> <sha9>`` per line. Once per head.
REPAIR_LOG = "repaired.txt"
#: Converging-lane rework attempts, ``<lane>`` per line. Capped per *lane*, not
#: per head: the point is to stop a lane that keeps looking convergent from
#: spending the whole fleet's budget one head at a time.
LANE_REWORK_LOG = "reworked_lane.txt"

_LOGS: dict[str, str] = {
    "regate": REGATE_LOG,
    "rework": REWORK_LOG,
    "rebase": REBASE_LOG,
    "repair": REPAIR_LOG,
    "rework_lane": LANE_REWORK_LOG,
}

_RECORD_RE = re.compile(r"^(?P<lane>\S+)\s+(?P<head>[0-9a-fA-F]{7,40})$")
_LANE_RECORD_RE = re.compile(r"^(?P<lane>\S+)$")


class RoutingError(RuntimeError):
    """A routing action cannot be carried out (no gh, no worktree, bad git)."""


def ops_home() -> Path:
    """``$FLEET_OPS_HOME`` or the drivers' default.

    The env var is the reconciler's own, not ``agent_fleet_home``: the counter
    files are shared with ``pr_triage.py`` and the reconciler, and giving them a
    new home would start a second, divergent count for the same lane.
    """
    return Path(os.environ.get("FLEET_OPS_HOME") or DEFAULT_OPS_HOME).expanduser()


def _read(path: Path) -> list[str]:
    """The records in *path*, empty when it does not exist.

    A missing file means nothing has been spent yet, which is the state a fresh
    lane is in. A file that exists but cannot be read raises instead of reading
    as empty: a permissions problem reported as a spent budget of zero would
    hand every lane a fresh budget and defeat the cap entirely.
    """
    if not path.exists():
        return []
    try:
        return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError as exc:
        raise RoutingError(f"could not read routing counter {path}: {exc}") from exc


def _count(path: Path, lane: str, head: str) -> int:
    total = 0
    for line in _read(path):
        match = _RECORD_RE.match(line.strip())
        if match is None:
            continue
        total += int(match.group("lane") == lane and _same_head(match.group("head"), head))
    return total


def _count_lane(path: Path, lane: str) -> int:
    total = 0
    for line in _read(path):
        match = _LANE_RECORD_RE.match(line.strip())
        total += int(match is not None and match.group("lane") == lane)
    return total


def _same_head(left: str, right: str) -> bool:
    """Compare two shas over the characters both carry (7 is the floor)."""
    width = min(len(left), len(right))
    return width >= 7 and left[:width].lower() == right[:width].lower()


def read_counters(lane: str, head: str, *, home: Path | None = None) -> RouteCounters:
    """What this head and lane have already spent on each action."""
    root = home or ops_home()
    return RouteCounters(
        regate_at_head=_count(root / REGATE_LOG, lane, head),
        rework_at_head=_count(root / REWORK_LOG, lane, head),
        rebase_at_head=_count(root / REBASE_LOG, lane, head),
        repair_at_head=_count(root / REPAIR_LOG, lane, head),
        rework_at_lane=_count_lane(root / LANE_REWORK_LOG, lane),
    )


def record_attempt(kind: str, lane: str, head: str = "", *, home: Path | None = None) -> bool:
    """Charge one attempt of *kind* against this head (or lane). Returns True if recorded.

    Called *before* the action runs, not after: a re-gate that the fleet launches
    but the machine drops still cost a gate run, and a cap that only counted
    successful actions would hand the same head an unbounded number of them.
    ``kind`` is ``regate``/``rework``/``rebase``/``repair``/``rework_lane``.
    """
    name = _LOGS.get(kind)
    if name is None:
        raise ValueError(f"unknown attempt kind {kind!r}; expected one of {sorted(_LOGS)}")
    root = home or ops_home()
    path = root / name
    record = lane if kind == "rework_lane" else f"{lane} {head}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(record + "\n")
    except OSError as exc:
        # Failing to record would let the next pass hand out the same budget
        # again, so this is fatal to the action rather than logged: a routing
        # decision that cannot be counted must not be taken.
        raise RoutingError(f"could not record {kind} attempt for {lane}@{head}: {exc}") from exc
    return True
