"""Spine configuration for fleet orchestration (worktrees, scope, cross-cutting groups)."""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SpineConfig:
    """Frozen, typed configuration for the fleet orchestration spine."""

    # Worktree base directory for per-run isolated worktrees.
    worktree_base: Path

    # GitHub PR label applied to draft PRs (verify retries exhausted).
    pr_draft_label: str

    # GitHub PR label applied to ready PRs (normal flow).
    pr_ready_label: str

    # Branch name prefix, e.g. "fleet" → branches like "fleet/{persona}/{issue}-{run_id}".
    branch_prefix: str

    # Label prefix for coop sibling issues, e.g. "agent-coop" → "agent-coop/{persona}".
    coop_label_prefix: str

    # Label prefix applied to coop parent issues, e.g. "agent-coop-parent".
    coop_parent_label_prefix: str

    # Persona scope allowlist: maps persona name → tuple of allowed path prefixes.
    # Empty tuple means unrestricted (all paths allowed).
    persona_scope_allowlist: dict[str, tuple[str, ...]]

    # Groups of directory prefixes that define persona boundaries.
    # If an issue's allowed_paths spans multiple prefixes from any one group,
    # the planner forces decomposition (cross-cutting override).
    cross_cutting_groups: tuple[frozenset[str], ...]

    # Path prefixes considered fleet/verifier infrastructure.
    # Agents are forbidden from modifying files under these prefixes.
    fleet_critical_prefixes: tuple[str, ...]

    # ------------------------------------------------------------------
    # Design-review configuration (all default to OFF / safe defaults)
    # ------------------------------------------------------------------

    # Master switch — DESIGN_REVIEW phase is excluded from the default phase
    # graph unless this is True.  False by default; never auto-enabled.
    design_review_enabled: bool = False

    # Glob patterns (fnmatch-style) matched against repo-relative changed-file
    # paths.  DESIGN_REVIEW condition fires only when at least one changed file
    # matches.  Defaults to the SilphCo visual surface.
    design_visual_surface_globs: tuple[str, ...] = ("frontend/**",)

    # Minimum average score across all rubric dimensions to allow a PR to
    # proceed without requiring human review.  Below this threshold the
    # policy gate fires needs_work (advisory) or block (hard gate) depending
    # on verdict.  Sane default: 70/100.
    design_score_threshold: int = 70

    # Key used to look up the injected AgentExecutor for DESIGN_REVIEW.
    # The entrypoint registers a VisionExecutor under this key.
    design_executor_key: str = "vision"

    # Path to the rubric Markdown file (relative or absolute).  Empty string
    # means "use the bundled default rubric".
    design_rubric_path: str = ""

    # ------------------------------------------------------------------
    # Factory methods
    # ------------------------------------------------------------------

    @classmethod
    def defaults(cls) -> "SpineConfig":
        """Return a SpineConfig populated with exactly the pre-refactor hardcoded values.

        Calling SpineConfig.defaults() must reproduce the exact same behaviour
        as the code before this refactor. Every field value here is sourced
        directly from the original literal in the file it replaced.
        """
        return cls(
            # From fleet/runner.py: _WORKTREE_BASE = Path("/tmp/agent-worktrees")
            worktree_base=Path("/tmp/agent-fleet-worktrees"),

            # From fleet/runner.py: labels=["fleet-draft"] (draft PR on verify exhaustion)
            pr_draft_label="fleet-draft",

            # From fleet/runner.py: labels=["fleet-auto"] (ready PR)
            pr_ready_label="fleet-auto",

            # From fleet/runner.py: f"fleet/{persona_name}/..."
            branch_prefix="fleet",

            # From fleet/runner.py: f"agent-coop/{persona}" used in _maybe_dispatch_coop
            coop_label_prefix="agent-coop",

            # From fleet/runner.py: f"agent-coop-parent/{issue_number}" + checks for
            # lbl.startswith("agent-coop-parent/")
            coop_parent_label_prefix="agent-coop-parent",

            # From agents/agents/constants.py: PERSONA_SCOPE_ALLOWLIST
            persona_scope_allowlist={},

            cross_cutting_groups=(
                frozenset({"frontend/", "backend/"}),
                frozenset({"frontend/", "api/"}),
            ),

            fleet_critical_prefixes=(
                ".github/workflows/",
            ),

            # Design review: DISABLED by default — all safe sentinel values.
            design_review_enabled=False,
            design_visual_surface_globs=("frontend/**",),
            design_score_threshold=70,
            design_executor_key="vision",
            design_rubric_path="",
        )

    @classmethod
    def _from_toml_uncached(cls, config_path: Path | None = None) -> "SpineConfig":
        """Parse a SpineConfig from *config_path* without caching.

        This is the raw implementation.  Callers should use :meth:`from_toml`
        which memoises results per resolved path.
        """
        defaults = cls.defaults()

        resolved = config_path
        if resolved is None:
            resolved = Path(__file__).parent.parent / "fleet_config.toml"

        if not resolved.exists():
            return defaults

        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore[no-reassign]

        try:
            with resolved.open("rb") as fh:
                data = tomllib.load(fh)
        except Exception:
            return defaults

        spine = data.get("spine", {})
        if not isinstance(spine, dict):
            spine = {}

        design = data.get("design", {})
        if not isinstance(design, dict):
            design = {}

        # Helper: extract from spine table with type-checked fallback to default.
        def _get(key: str, default: Any) -> Any:
            return spine.get(key, default)

        # Helper: extract from design table with type-checked fallback.
        def _dget(key: str, default: Any) -> Any:
            return design.get(key, default)

        # worktree_base: string → Path
        wt_raw = _get("worktree_base", None)
        worktree_base = Path(wt_raw) if isinstance(wt_raw, str) else defaults.worktree_base

        pr_draft_label = _get("pr_draft_label", defaults.pr_draft_label)
        if not isinstance(pr_draft_label, str):
            pr_draft_label = defaults.pr_draft_label

        pr_ready_label = _get("pr_ready_label", defaults.pr_ready_label)
        if not isinstance(pr_ready_label, str):
            pr_ready_label = defaults.pr_ready_label

        branch_prefix = _get("branch_prefix", defaults.branch_prefix)
        if not isinstance(branch_prefix, str):
            branch_prefix = defaults.branch_prefix

        coop_label_prefix = _get("coop_label_prefix", defaults.coop_label_prefix)
        if not isinstance(coop_label_prefix, str):
            coop_label_prefix = defaults.coop_label_prefix

        coop_parent_label_prefix = _get("coop_parent_label_prefix", defaults.coop_parent_label_prefix)
        if not isinstance(coop_parent_label_prefix, str):
            coop_parent_label_prefix = defaults.coop_parent_label_prefix

        # persona_scope_allowlist: {persona: [path, ...]}
        psa_raw = _get("persona_scope_allowlist", None)
        if isinstance(psa_raw, dict) and all(
            isinstance(k, str) and isinstance(v, list) for k, v in psa_raw.items()
        ):
            persona_scope_allowlist: dict[str, tuple[str, ...]] = {
                k: tuple(v) for k, v in psa_raw.items()
            }
        else:
            persona_scope_allowlist = defaults.persona_scope_allowlist

        # cross_cutting_groups: [[path, ...], ...]
        ccg_raw = _get("cross_cutting_groups", None)
        if isinstance(ccg_raw, list) and all(
            isinstance(g, list) and all(isinstance(p, str) for p in g)
            for g in ccg_raw
        ):
            cross_cutting_groups: tuple[frozenset[str], ...] = tuple(
                frozenset(g) for g in ccg_raw
            )
        else:
            cross_cutting_groups = defaults.cross_cutting_groups

        # fleet_critical_prefixes: [path, ...]
        fcp_raw = _get("fleet_critical_prefixes", None)
        if isinstance(fcp_raw, list) and all(isinstance(p, str) for p in fcp_raw):
            fleet_critical_prefixes: tuple[str, ...] = tuple(fcp_raw)
        else:
            fleet_critical_prefixes = defaults.fleet_critical_prefixes

        # ------------------------------------------------------------------
        # [design] table fields — all default to OFF
        # ------------------------------------------------------------------

        design_review_enabled_raw = _dget("enabled", defaults.design_review_enabled)
        design_review_enabled = bool(design_review_enabled_raw) if isinstance(design_review_enabled_raw, (bool, int)) else defaults.design_review_enabled

        dvsg_raw = _dget("visual_surface_globs", None)
        if isinstance(dvsg_raw, list) and all(isinstance(p, str) for p in dvsg_raw):
            design_visual_surface_globs: tuple[str, ...] = tuple(dvsg_raw)
        else:
            design_visual_surface_globs = defaults.design_visual_surface_globs

        dst_raw = _dget("score_threshold", defaults.design_score_threshold)
        design_score_threshold = int(dst_raw) if isinstance(dst_raw, int) else defaults.design_score_threshold

        dek_raw = _dget("executor_key", defaults.design_executor_key)
        design_executor_key = str(dek_raw) if isinstance(dek_raw, str) else defaults.design_executor_key

        drp_raw = _dget("rubric_path", defaults.design_rubric_path)
        design_rubric_path = str(drp_raw) if isinstance(drp_raw, str) else defaults.design_rubric_path

        return cls(
            worktree_base=worktree_base,
            pr_draft_label=pr_draft_label,
            pr_ready_label=pr_ready_label,
            branch_prefix=branch_prefix,
            coop_label_prefix=coop_label_prefix,
            coop_parent_label_prefix=coop_parent_label_prefix,
            persona_scope_allowlist=persona_scope_allowlist,
            cross_cutting_groups=cross_cutting_groups,
            fleet_critical_prefixes=fleet_critical_prefixes,
            design_review_enabled=design_review_enabled,
            design_visual_surface_globs=design_visual_surface_globs,
            design_score_threshold=design_score_threshold,
            design_executor_key=design_executor_key,
            design_rubric_path=design_rubric_path,
        )

    @classmethod
    def from_toml(cls, config_path: Path | None = None) -> "SpineConfig":
        """Return a cached :class:`SpineConfig` for *config_path*.

        Parses and returns a :class:`SpineConfig` from *config_path* (or the
        default ``fleet_config.toml`` sibling when *config_path* is ``None``).
        Results are memoised per resolved path so that hot-path callers
        (``merge_gate.out_of_scope_files``, ``verify_checks.check_no_verifier_self_modify``,
        etc.) do not re-parse TOML on every invocation.

        Use :func:`clear_spine_config_cache` in tests that monkeypatch the TOML
        file so the next ``from_toml()`` call picks up the new contents.
        """
        resolved = config_path if config_path is not None else (
            Path(__file__).parent.parent / "fleet_config.toml"
        )
        return _cached_from_toml(resolved)


# ---------------------------------------------------------------------------
# Module-level cache (keyed by resolved Path)
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=8)
def _cached_from_toml(resolved: Path) -> SpineConfig:
    """Parse and cache a :class:`SpineConfig` for *resolved* path.

    Keyed by the resolved ``Path`` object so each distinct config file path
    gets its own cache entry.  Call :func:`clear_spine_config_cache` to
    invalidate (e.g. in tests that write a temporary ``fleet_config.toml``).
    """
    return SpineConfig._from_toml_uncached(resolved)


def clear_spine_config_cache() -> None:
    """Invalidate the ``SpineConfig.from_toml`` parse cache.

    Call this in tests that monkeypatch or write a new ``fleet_config.toml``
    so the next ``SpineConfig.from_toml()`` call re-reads the file.
    """
    _cached_from_toml.cache_clear()
