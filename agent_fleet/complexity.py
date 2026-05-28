"""Complexity-driven runtime derivation.

The user declares a single ``complexity`` level; this module maps it to the
four runtime parameters consumed by the dispatcher and runner.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

logger = logging.getLogger(__name__)

Complexity = Literal["LOW", "MED", "HIGH"]
_VALID: frozenset[str] = frozenset({"LOW", "MED", "HIGH"})


class TokenCeilingExceeded(Exception):
    """Raised when cumulative token usage exceeds the complexity ceiling.

    Attributes:
        declared_complexity: The complexity level declared on the task.
        observed_total_tokens: Cumulative tokens at the point of abort.
        ceiling: The token ceiling that was breached.
    """

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


def coerce_complexity(value: str | None) -> Complexity:
    """Validate and normalise a raw complexity string; default to 'MED'.

    Raises ``ValueError`` for non-None values that are not in {LOW, MED, HIGH}.
    """
    if value is None:
        return "MED"
    upper = str(value).strip().upper()
    if upper not in _VALID:
        raise ValueError(
            f"Invalid complexity {value!r}. Must be one of {sorted(_VALID)}."
        )
    return cast("Complexity", upper)


def derive_runtime(complexity: Complexity | str | None) -> RuntimeConfig:
    """Return the ``RuntimeConfig`` for *complexity*.

    Accepts ``None`` (treated as ``'MED'``) or any casing of the three valid
    levels.  Raises ``ValueError`` for other inputs.
    """
    level = coerce_complexity(complexity)
    return _RUNTIME_MAP[level]


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
