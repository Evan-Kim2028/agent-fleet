"""Per-gate metrics: the record that makes convergence observable.

A gate run's interesting question is not "did it approve" but "how did the
failing set move". ``max_fix_rounds`` is only a safety net, so without a
per-round record a run that stalled after three rounds of one-test-at-a-time
progress looks identical to one that converged in a single round.

Every gate run appends one :class:`GateMetrics` row to
``~/.agent-fleet/gate/metrics.jsonl``, and ``agent-fleet gate metrics`` folds
those rows into a table. The row carries the candidate -> confirmed funnel, the
convergence decision per round, and the terminal outcome.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from agent_fleet.fleet_paths import agent_fleet_home

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

logger = logging.getLogger(__name__)

METRICS_DIRNAME = "gate"
METRICS_FILENAME = "metrics.jsonl"

# Terminal states, in the order a run can reach them. ``converged`` is the only
# success; everything else escalates to a human.
OUTCOME_CONVERGED = "converged"
OUTCOME_STALLED = "stalled"
OUTCOME_NO_PUSH = "no-push"
OUTCOME_TESTS_BROKEN = "tests-broken"
OUTCOME_UNTESTABLE_UNRESOLVED = "untestable-unresolved"
OUTCOME_UNTESTABLE_NEEDS_REVIEW = "untestable-needs-review"
OUTCOME_CAP = "cap"


def metrics_path() -> Path:
    """``~/.agent-fleet/gate/metrics.jsonl`` (honours ``AGENT_FLEET_HOME``)."""
    return agent_fleet_home() / METRICS_DIRNAME / METRICS_FILENAME


@dataclass
class RoundMetric:
    """One fix round's convergence measurement.

    ``failing`` is the count of failing test ids at this round's head; ``fixed``
    and ``new_failures`` are the set deltas against the previous round. A round
    counts as progress only when ``fixed > 0 and new_failures == 0``.
    """

    round: int
    head: str
    failing: int
    fixed: int = 0
    new_failures: int = 0

    @property
    def progressed(self) -> bool:
        return self.new_failures == 0 and self.fixed > 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class GateMetrics:
    """The full funnel and convergence trace for one gate run."""

    run_id: str
    repo: str
    pr: int
    start_sha: str
    outcome: str = ""
    candidates: int = 0
    confirmed: int = 0
    rejected: int = 0
    untestable: int = 0
    untestable_real: int = 0
    rounds: list[RoundMetric] = field(default_factory=list)
    head_sha: str = ""
    reasons: list[str] = field(default_factory=list)
    at: str = ""
    #: Per-agent-call parse state (lens/verify/judge). See GateCallRecord.
    calls: list[dict[str, object]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.at:
            self.at = datetime.now().isoformat(timespec="seconds")

    @property
    def failing_by_round(self) -> list[int]:
        return [r.failing for r in self.rounds]

    @property
    def round_count(self) -> int:
        return len(self.rounds)

    def to_dict(self) -> dict[str, object]:
        return {
            "at": self.at,
            "run_id": self.run_id,
            "repo": self.repo,
            "pr": self.pr,
            "start_sha": self.start_sha,
            "head_sha": self.head_sha,
            "outcome": self.outcome,
            "candidates": self.candidates,
            "confirmed": self.confirmed,
            "rejected": self.rejected,
            "untestable": self.untestable,
            "untestable_real": self.untestable_real,
            "rounds": [r.to_dict() for r in self.rounds],
            "failing_by_round": self.failing_by_round,
            "reasons": list(self.reasons),
            "calls": list(self.calls),
        }

    def append_metrics(self, path: Path | None = None) -> Path:
        """Append this row to the metrics JSONL. Never raises — logging must not
        fail a run that has already reached its verdict."""
        target = path or metrics_path()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(self.to_dict(), default=str))
                handle.write("\n")
        except (OSError, TypeError) as exc:
            logger.debug("gate metrics append failed: %s", exc)
        return target


def read_metrics(path: Path | None = None, *, limit: int | None = None) -> list[dict[str, object]]:
    """Read gate metric rows newest-last, tolerating a partial final line."""
    target = path or metrics_path()
    if not target.exists():
        return []
    rows: list[dict[str, object]] = []
    try:
        with target.open(encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except OSError as exc:
        logger.debug("gate metrics read failed: %s", exc)
        return []
    if limit is not None and limit > 0:
        return rows[-limit:]
    return rows


def _as_int(value: object) -> int:
    """Coerce an untrusted on-disk metric value to int (bad input reads as 0)."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def _as_round_counts(value: object) -> list[int]:
    """Coerce the per-round failing counts to a list of ints."""
    if not isinstance(value, list):
        return []
    return [_as_int(item) for item in value]


def render_metrics_table(rows: Iterable[dict[str, object]]) -> str:
    """Render metric rows as a fixed-column table. Pure: rows -> text."""
    materialized = list(rows)
    if not materialized:
        return "No gate runs recorded yet."
    header = (
        f"{'AT':<19}  {'PR':>4}  {'CAND':>4}  {'CONF':>4}  {'REJ':>3}  "
        f"{'UTEST':>5}  {'RND':>3}  {'FAILING':<12}  OUTCOME"
    )
    lines = [header, "-" * len(header)]
    for row in materialized:
        failing = _as_round_counts(row.get("failing_by_round"))
        failing_str = ",".join(str(f) for f in failing) if failing else "-"
        lines.append(
            f"{str(row.get('at', '?'))[:19]:<19}  {row.get('pr', '?')!s:>4}  "
            f"{_as_int(row.get('candidates')):>4}  {_as_int(row.get('confirmed')):>4}  "
            f"{_as_int(row.get('rejected')):>3}  {_as_int(row.get('untestable')):>5}  "
            f"{len(failing):>3}  {failing_str:<12}  {row.get('outcome', '?')!s}"
        )
    return "\n".join(lines)


def summarize_rows(rows: Iterable[dict[str, object]]) -> dict[str, object]:
    """Fold metric rows into aggregate counts for a quick health read."""
    materialized = list(rows)
    outcomes: dict[str, int] = {}
    rounds_total = 0
    candidates = confirmed = 0
    for row in materialized:
        outcome = str(row.get("outcome", "unknown"))
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        rounds_total += len(_as_round_counts(row.get("failing_by_round")))
        candidates += _as_int(row.get("candidates"))
        confirmed += _as_int(row.get("confirmed"))
    return {
        "runs": len(materialized),
        "outcomes": dict(sorted(outcomes.items())),
        "rounds_total": rounds_total,
        "candidates_total": candidates,
        "confirmed_total": confirmed,
        "approval_rate": (
            round(outcomes.get(OUTCOME_CONVERGED, 0) / len(materialized), 3)
            if materialized
            else 0.0
        ),
    }
