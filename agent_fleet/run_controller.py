"""Run Controller seam: closed-loop circuit breaker for the VERIFY/FIX spiral.

ThresholdController inspects live token usage and verify-attempt counts before
each FIX iteration, halting the loop early to avoid runaway token spend.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Protocol


class ControlDecision(enum.StrEnum):
    CONTINUE = "continue"
    HALT = "halt"
    ABANDON = "abandon"


@dataclass(frozen=True)
class RunMetrics:
    verify_attempts: int
    fix_token_total: int
    total_tokens: int
    fix_phase_ratio: float
    cost_alerts: tuple[str, ...]


@dataclass(frozen=True)
class ControllerPolicy:
    max_fix_ratio: float = 0.6
    halt_after_attempts: int = 3
    halt_on_alert: bool = True


class RunController(Protocol):
    def before_fix(self, m: RunMetrics, policy: ControllerPolicy) -> ControlDecision: ...


def _build_run_metrics(
    usage_rollup: dict[str, Any] | None,
    verify_attempts: int,
) -> RunMetrics:
    """Construct RunMetrics from a usage_rollup snapshot and attempt counter."""
    from agent_fleet.observability.run_metrics import (
        build_cost_alerts,
        fix_phase_ratio,
        phase_token_counts,
    )

    ratio = fix_phase_ratio(usage_rollup)
    alerts = tuple(build_cost_alerts(usage_rollup=usage_rollup, verify_attempts=verify_attempts))
    total_tokens, fix_token_total = phase_token_counts(usage_rollup)

    return RunMetrics(
        verify_attempts=verify_attempts,
        fix_token_total=fix_token_total,
        total_tokens=total_tokens,
        fix_phase_ratio=ratio,
        cost_alerts=alerts,
    )


@dataclass
class ThresholdController:
    """Halt when FIX phase tokens dominate, attempts exceed the ceiling, or an alert fires."""

    def before_fix(self, m: RunMetrics, policy: ControllerPolicy) -> ControlDecision:
        # Two or more verify attempts means at least one FIX has already run,
        # so we have real ratio signal; skip the check on the very first fix.
        if m.verify_attempts >= 2 and m.fix_phase_ratio > policy.max_fix_ratio:
            return ControlDecision.HALT
        if m.verify_attempts >= policy.halt_after_attempts:
            return ControlDecision.HALT
        if policy.halt_on_alert and "fix_phase_token_ratio_high" in m.cost_alerts:
            return ControlDecision.HALT
        return ControlDecision.CONTINUE
