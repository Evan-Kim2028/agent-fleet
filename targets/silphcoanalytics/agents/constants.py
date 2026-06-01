"""Shared constants for agent dispatch + watcher.

Anywhere two modules in `agents/agents/` were duplicating a regex or magic
number, the source of truth lives here. Keep this file small.

NOTE: fleet/ must NOT import from this module. The persona scope map has been
migrated to `.agent-fleet.yaml` (`critical_path_prefixes`, `persona_scope_allowlist`) for
provider/repo-neutral operation. PERSONA_SCOPE_ALLOWLIST is preserved here
ONLY for backward compatibility with agents/agents/ callers (dispatch.py,
watcher.py) that have not yet been migrated.
"""

PERSONA_PATTERN = r"/agent\s+--persona\s+(\S+)"

KNOWN_PERSONAS = frozenset({
    "backend",
    "frontend",
    "data",
    "pokemon_analyst",
    "security_qa",
})

IGNORED_CI_CHECKS = frozenset({"kimi pr analysis", "pr-analyzer"})

GH_SUBPROCESS_TIMEOUT_S = 30

# Directory prefixes each persona is allowed to touch.
# Files outside these prefixes are flagged in the PR body for human review.
# Empty list means no restriction.
# DEPRECATED for new fleet/ code: use SpineConfig.defaults().persona_scope_allowlist instead.
PERSONA_SCOPE_ALLOWLIST: dict[str, list[str]] = {
    "backend": ["api/", "pipeline/src/models/", "pipeline/src/queries/"],
    "frontend": ["frontend/"],
    "data": ["pipeline/", "data/"],
    "pokemon_analyst": ["pipeline/", "data/", "research/"],
    "security_qa": [],
}
