"""Complexity-driven runtime derivation.

The user declares a single ``complexity`` level; this module maps it to the
four runtime parameters consumed by the dispatcher and runner.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

logger = logging.getLogger(__name__)

Complexity = Literal["LOW", "MED", "HIGH"]
_VALID: frozenset[str] = frozenset({"LOW", "MED", "HIGH"})


@dataclass(frozen=True)
class TokenCeilingBreach:
    """Observed token usage above the declared complexity ceiling (metric only)."""

    declared_complexity: str
    observed_total_tokens: int
    ceiling: int
    over_by: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "declared_complexity": self.declared_complexity,
            "observed_total_tokens": self.observed_total_tokens,
            "ceiling": self.ceiling,
            "over_by": self.over_by,
            "efficiency_ratio": round(self.observed_total_tokens / self.ceiling, 3),
        }


class TokenCeilingExceeded(Exception):
    """Legacy exception; ceilings are recorded as metrics, not raised mid-run."""

    def __init__(
        self,
        *,
        declared_complexity: str,
        observed_total_tokens: int,
        ceiling: int,
    ) -> None:
        self.declared_complexity = declared_complexity
        self.observed_total_tokens = observed_total_tokens
        self.ceiling = ceiling
        super().__init__(
            f"Token ceiling exceeded: {observed_total_tokens} > {ceiling} "
            f"(complexity={declared_complexity})"
        )


def observe_token_ceiling(
    *,
    token_ceiling: int,
    declared_complexity: str,
) -> TokenCeilingBreach | None:
    """Return breach details when usage exceeds *token_ceiling*; does not abort the run."""
    from agent_fleet.observability.context import get_run_log

    run_log = get_run_log()
    if run_log is None:
        return None
    totals = dict(run_log._usage_totals)
    observed = sum(totals.values())
    if observed <= token_ceiling:
        return None
    return TokenCeilingBreach(
        declared_complexity=declared_complexity,
        observed_total_tokens=observed,
        ceiling=token_ceiling,
        over_by=observed - token_ceiling,
    )


@dataclass(frozen=True)
class RuntimeConfig:
    """Derived runtime parameters from a task complexity level."""

    pipeline: str
    retries: int
    token_ceiling: int
    loadout_size: str


_RUNTIME_MAP: dict[str, RuntimeConfig] = {
    "LOW": RuntimeConfig(
        pipeline="simple",
        retries=1,
        token_ceiling=1_000_000,
        loadout_size="minimal",
    ),
    "MED": RuntimeConfig(
        pipeline="code_review",
        retries=1,
        token_ceiling=5_000_000,
        loadout_size="standard",
    ),
    "HIGH": RuntimeConfig(
        pipeline="code_review",
        retries=2,
        token_ceiling=20_000_000,
        loadout_size="full",
    ),
}


_SCOPE_HIGH: frozenset[str] = frozenset(
    {
        "refactor",
        "migrate",
        "migration",
        "rewrite",
        "architect",
        "architecture",
        "redesign",
        "overhaul",
        "restructure",
        "cross-cutting",
        "cross cutting",
        "system-wide",
        "system wide",
        "entire codebase",
        "multiple services",
    }
)

_SCOPE_LOW: frozenset[str] = frozenset(
    {
        "fix typo",
        "rename",
        "update comment",
        "update docs",
        "add comment",
        "bump version",
        "format",
        "lint",
        "one-liner",
        "single line",
        "small fix",
        "minor fix",
        "trivial",
    }
)

_WORDS_HIGH = 60  # goal word count threshold → at least MED
_WORDS_VERY_HIGH = 120  # goal word count threshold → HIGH
_FILES_MED = 3  # changed_files count thresholds
_FILES_HIGH = 8


def classify_complexity(
    goal: str,
    changed_files: list[str] | None = None,
) -> Complexity:
    """Derive a Complexity tier from *goal* text and optional scope hints.

    Rules applied in order (first match wins):
    1. Any HIGH scope keyword in goal → HIGH.
    2. File count >= _FILES_HIGH or goal word count >= _WORDS_VERY_HIGH → HIGH.
    3. Any LOW scope keyword in goal AND file count <= 1 AND word count < _WORDS_HIGH → LOW.
    4. File count >= _FILES_MED or word count >= _WORDS_HIGH → MED.
    5. Default → LOW.
    """
    goal_lower = goal.lower()
    words = len(goal.split())
    n_files = len(changed_files) if changed_files else 0

    for kw in _SCOPE_HIGH:
        if kw in goal_lower:
            return "HIGH"

    if n_files >= _FILES_HIGH or words >= _WORDS_VERY_HIGH:
        return "HIGH"

    for kw in _SCOPE_LOW:
        if kw in goal_lower and n_files <= 1 and words < _WORDS_HIGH:
            return "LOW"

    if n_files >= _FILES_MED or words >= _WORDS_HIGH:
        return "MED"

    return "LOW"


def coerce_complexity(value: str | None) -> Complexity:
    """Validate and normalise a raw complexity string; default to 'MED'.

    Raises ``ValueError`` for non-None values that are not in {LOW, MED, HIGH}.
    """
    if value is None:
        return "MED"
    upper = str(value).strip().upper()
    if upper not in _VALID:
        raise ValueError(f"Invalid complexity {value!r}. Must be one of {sorted(_VALID)}.")
    return cast("Complexity", upper)


def derive_runtime(
    complexity: Complexity | str | None,
    *,
    tier_overrides: dict[str, dict[str, Any]] | None = None,
) -> RuntimeConfig:
    """Return the ``RuntimeConfig`` for *complexity*.

    Accepts ``None`` (treated as ``'MED'``) or any casing of the three valid
    levels.  Raises ``ValueError`` for other inputs.

    *tier_overrides* may supply per-tier field overrides from fleet/repo config
    (e.g. ``{"LOW": {"token_ceiling": 2_000_000}}``).  Only the keys present in
    the override are changed; the Python defaults fill the rest.
    """
    level = coerce_complexity(complexity)
    base = _RUNTIME_MAP[level]
    if tier_overrides and level in tier_overrides:
        raw = tier_overrides[level]
        base = RuntimeConfig(
            pipeline=str(raw["pipeline"]) if "pipeline" in raw else base.pipeline,
            retries=int(raw["retries"]) if "retries" in raw else base.retries,
            token_ceiling=int(raw["token_ceiling"])
            if "token_ceiling" in raw
            else base.token_ceiling,
            loadout_size=str(raw["loadout_size"]) if "loadout_size" in raw else base.loadout_size,
        )
    return base


def is_actionable_stderr(stderr: str, written_files: tuple[str, ...] | list[str]) -> bool:
    """Return True when *stderr* is non-empty AND mentions a file the agent wrote.

    This implements the LOW-complexity retry gate: a generic stderr (e.g. a
    deprecation warning that doesn't name any changed file) does not warrant a
    retry budget spend.
    """
    if not stderr or not stderr.strip():
        return False
    if not written_files:
        return False
    for path in written_files:
        # Check both the full path and the basename so partial matches work.
        basename = Path(path).name
        if basename and basename in stderr:
            return True
        if path and path in stderr:
            return True
    return False
