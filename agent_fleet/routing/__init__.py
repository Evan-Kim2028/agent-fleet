"""Post-gate routing: decide what happens to a PR after the gate rules on it.

Until this package existed the policy lived in
``ops/vps/orchestrator/fleet_reconcile.sh`` as two EREs, a counter file, and a
``while :`` loop. Splitting the decision from the doing makes the whole table
testable without a repository or a model:

* :mod:`agent_fleet.routing.policy` — the pure decision. No IO at all.
* :mod:`agent_fleet.routing.counters` — the per-head attempt budgets, in the
  reconciler's own on-disk format so ``pr_triage.py`` keeps reading them.
* :mod:`agent_fleet.routing.executor` — the two agent-backed actions, rebase and
  repair, and the fail-closed escalation they leave behind.
"""

from __future__ import annotations

from agent_fleet.routing.counters import RoutingError, read_counters, record_attempt
from agent_fleet.routing.executor import AgentResult, Mode
from agent_fleet.routing.policy import (
    MAX_REGATE_PER_HEAD,
    MAX_REWORK_PER_LANE,
    Action,
    Decision,
    RouteCounters,
    Verdict,
    classify,
    decide,
    is_converging,
    last_verdict_line,
    round_counts,
)

__all__ = [
    "MAX_REGATE_PER_HEAD",
    "MAX_REWORK_PER_LANE",
    "Action",
    "AgentResult",
    "Decision",
    "Mode",
    "RouteCounters",
    "RoutingError",
    "Verdict",
    "classify",
    "decide",
    "is_converging",
    "last_verdict_line",
    "read_counters",
    "record_attempt",
    "round_counts",
]
