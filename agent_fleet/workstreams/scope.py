"""Scope overlap detection for parallel workstream batches."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_fleet.repo import RepoConfig
    from agent_fleet.workstreams.config import WorkstreamItem


def scope_prefixes_for_persona(repo: RepoConfig, persona: str) -> frozenset[str]:
    allowlist = repo.persona_scope_allowlist.get(persona)
    if not allowlist:
        return frozenset()
    return frozenset(str(prefix).rstrip("/") for prefix in allowlist)


def _prefixes_overlap(a: str, b: str) -> bool:
    return a == b or a.startswith(f"{b}/") or b.startswith(f"{a}/")


def find_scope_overlaps(
    repo: RepoConfig,
    personas: list[str],
) -> list[tuple[str, str, str]]:
    """Return (persona_a, persona_b, shared_prefix) for overlapping allowlists."""
    overlaps: list[tuple[str, str, str]] = []
    for i, persona_a in enumerate(personas):
        prefixes_a = scope_prefixes_for_persona(repo, persona_a)
        for persona_b in personas[i + 1 :]:
            prefixes_b = scope_prefixes_for_persona(repo, persona_b)
            for prefix_a in prefixes_a:
                for prefix_b in prefixes_b:
                    if _prefixes_overlap(prefix_a, prefix_b):
                        overlaps.append((persona_a, persona_b, prefix_a))
                        break
    return overlaps


def validate_parallel_batch(
    repo: RepoConfig,
    items: list[WorkstreamItem],
    *,
    sequential_stack: bool,
) -> None:
    """Raise ValueError when parallel dispatch would share scope prefixes."""
    if not sequential_stack or len(items) <= 1:
        return
    personas = [item.persona for item in items]
    overlaps = find_scope_overlaps(repo, personas)
    if not overlaps:
        return
    lines = [f"{a} ↔ {b} (prefix {prefix!r})" for a, b, prefix in overlaps[:5]]
    extra = len(overlaps) - len(lines)
    suffix = f" (+{extra} more)" if extra > 0 else ""
    raise ValueError(
        "Parallel workstream dispatch blocked by overlapping persona scopes: "
        + "; ".join(lines)
        + suffix
        + ". Run sequentially or adjust persona_scope_allowlist."
    )
