"""Declarative phase graph for the fleet engine.

Note on DESIGN_REVIEW:
  The DESIGN_REVIEW phase is included in ``default_phase_graph()`` only when
  ``design_review_enabled=True`` is explicitly requested.  By default it is
  absent so the phase graph is unchanged from the pre-extraction straight-line
  sequence.  See ``fleet.spine_config.SpineConfig`` for the controlling flags.


Provides:
  - ``PhaseSpec``    — description of one phase in the pipeline.
  - ``PhaseGraph``   — ordered, dependency-aware container of PhaseSpecs.
  - ``default_phase_graph()`` — the canonical PLAN→…→OPEN_PR graph that
    exactly replicates the pre-extraction straight-line sequence in runner.py.

Design contract:
  - The graph is purely declarative; it does NOT call handlers itself.
  - ``FleetRunner.run()`` iterates the graph and dispatches each phase to its
    handler via ``AgentExecutor``.
  - ``default_phase_graph()`` produces the same phase order, same retry bounds,
    and the same conditional TECH_LEAD as the pre-extraction runner.
  - The graph is injectable: ``FleetRunner.__init__(..., phase_graph=...)`` so
    tests and future PR lanes can substitute custom graphs.

Retry semantics:
  ``max_retries`` is per-phase; 0 means "run once, no retries on failure".
  The retry behaviour within VERIFY is still governed by
  ``FleetConfig.max_verify_retries`` (the loop counter lives in the runner).
  ``PhaseSpec.max_retries`` is a graph-level hint for future use; the runner
  currently only honours it for the VERIFY phase.

Condition predicates:
  ``condition`` is an optional ``Callable[[Any], bool]``.  When present the
  runner calls it with the current run context (a ``PhaseRunContext``) before
  dispatching; if it returns False the phase is skipped.  The TECH_LEAD
  phase uses this to replicate the old ``_should_trigger()`` guard.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any, Callable


# ---------------------------------------------------------------------------
# PhaseRunContext — thin context bag passed to condition predicates
# ---------------------------------------------------------------------------


@dataclass
class PhaseRunContext:
    """Minimal mutable context that condition predicates receive.

    The runner populates this incrementally as phases complete; predicates
    should only read fields that were set by earlier phases.
    """

    task_spec: Any = None           # fleet.contracts.task_spec.TaskSpec | None
    reviews: list[Any] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    # Additional fields may be added in future phases without breaking
    # existing predicates (they just won't read the new fields).


# ---------------------------------------------------------------------------
# PhaseSpec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PhaseSpec:
    """Description of one phase in the fleet pipeline.

    Attributes:
        name:        Unique phase identifier, e.g. ``"PLAN"``.  Must match
                     the strings used in ``_HEARTBEAT_PHASES`` and the runner's
                     ``_phase_handlers`` dispatch table.
        handler_key: Key into the runner's ``_phase_handlers`` dict.  Kept
                     separate from ``name`` so future aliases or renames
                     don't break the dispatch table.
        depends_on:  Names of phases that must have completed before this one
                     runs.  The runner validates this at graph-construction
                     time; cycles are rejected.
        max_retries: Maximum number of additional attempts on failure
                     (0 = run once).  Informational for most phases; the
                     runner's VERIFY loop uses this as its retry ceiling.
        condition:   Optional predicate ``(PhaseRunContext) -> bool``.  When
                     provided, the phase is skipped if it returns False.
                     ``None`` means "always run".
    """

    name: str
    handler_key: str
    depends_on: tuple[str, ...] = ()
    max_retries: int = 0
    condition: Callable[[PhaseRunContext], bool] | None = None


# ---------------------------------------------------------------------------
# PhaseGraph
# ---------------------------------------------------------------------------


class PhaseGraph:
    """Ordered, dependency-validated container of PhaseSpecs.

    Construction validates:
    1. No duplicate phase names.
    2. All ``depends_on`` references resolve to a preceding phase
       (topological / forward-only requirement — no cycles).

    Iteration (``__iter__``) yields phases in declaration order, which is
    the execution order the runner follows.
    """

    def __init__(self, phases: list[PhaseSpec]) -> None:
        seen: set[str] = set()
        for spec in phases:
            if spec.name in seen:
                raise ValueError(
                    f"PhaseGraph: duplicate phase name {spec.name!r}"
                )
            for dep in spec.depends_on:
                if dep not in seen:
                    raise ValueError(
                        f"PhaseGraph: phase {spec.name!r} depends on "
                        f"{dep!r} which has not been declared yet (forward "
                        f"reference or cycle)"
                    )
            seen.add(spec.name)
        self._phases = list(phases)

    def __iter__(self):
        return iter(self._phases)

    def __len__(self) -> int:
        return len(self._phases)

    def get(self, name: str) -> PhaseSpec | None:
        """Return the PhaseSpec with ``name``, or None."""
        for spec in self._phases:
            if spec.name == name:
                return spec
        return None


# ---------------------------------------------------------------------------
# Tech-lead condition predicate (mirrors tech_lead._should_trigger)
# ---------------------------------------------------------------------------


def _tech_lead_condition(ctx: PhaseRunContext) -> bool:
    """Return True when the Tech Lead phase should run.

    Exact mirror of ``fleet.tech_lead._should_trigger()``:
      - task_spec.risk_tier == HIGH
      - task_spec.critical_paths_touched non-empty
      - coordination_spec has non-empty merge_order
    """
    from agent_fleet.contracts.task_spec import RiskTier

    ts = ctx.task_spec
    if ts is None:
        return False
    if ts.risk_tier == RiskTier.HIGH:
        return True
    if ts.critical_paths_touched:
        return True
    if (
        ts.coordination_spec is not None
        and ts.coordination_spec.get("merge_order")
    ):
        return True
    return False


# ---------------------------------------------------------------------------
# default_phase_graph() — the canonical pipeline
# ---------------------------------------------------------------------------


def make_design_review_condition(
    visual_surface_globs: tuple[str, ...] = ("frontend/**",),
) -> Callable[[PhaseRunContext], bool]:
    """Return a condition predicate for the DESIGN_REVIEW phase.

    The predicate returns True iff at least one changed file matches any of
    the *visual_surface_globs* patterns.  This ensures DESIGN_REVIEW only
    fires when frontend/visual surfaces actually changed.

    Note: this predicate is only ever wired into the graph when
    ``SpineConfig.design_review_enabled=True``.  When disabled,
    ``default_phase_graph()`` does not include a DESIGN_REVIEW node at all.

    Args:
        visual_surface_globs: Glob patterns (fnmatch-style) against which
            repo-relative changed-file paths are matched.  Defaults to
            ``("frontend/**",)`` — the SilphCo visual surface.

    Returns:
        A ``Callable[[PhaseRunContext], bool]`` suitable for
        ``PhaseSpec.condition``.
    """

    def _condition(ctx: PhaseRunContext) -> bool:
        if not ctx.changed_files:
            # No changed files reported — skip rather than block.
            return False
        for f in ctx.changed_files:
            for pattern in visual_surface_globs:
                if fnmatch.fnmatch(f, pattern):
                    return True
        return False

    return _condition


def default_phase_graph(
    *,
    max_verify_retries: int = 3,
    design_review_enabled: bool = False,
    design_visual_surface_globs: tuple[str, ...] = ("frontend/**",),
) -> PhaseGraph:
    """Return the canonical fleet phase graph.

    Replicates the pre-extraction runner.py phase order exactly:
      PLAN → RESEARCH → SYNTHESIZE → IMPLEMENT → VERIFY → REVIEW
      → TECH_LEAD (conditional) → OPEN_PR

    DESIGN_REVIEW is **excluded by default** (``design_review_enabled=False``).
    Pass ``design_review_enabled=True`` (typically driven by
    ``SpineConfig.design_review_enabled``) to include it between VERIFY and
    REVIEW.  When included, its ``condition`` predicate gates it on whether
    any changed file matches *design_visual_surface_globs*.

    Args:
        max_verify_retries: forwarded to VERIFY's ``max_retries``.  Should
            match ``FleetConfig.max_verify_retries`` so the runner sees a
            consistent value.  Defaults to 3 (the original ``_MAX_VERIFY_RETRIES``
            constant).
        design_review_enabled: when True, insert a DESIGN_REVIEW phase between
            VERIFY and REVIEW.  Default False — no behaviour change for existing
            callers.
        design_visual_surface_globs: glob patterns passed to
            ``make_design_review_condition``.  Only used when
            ``design_review_enabled=True``.

    Returns:
        A ``PhaseGraph`` whose iteration order reproduces the pre-extraction
        straight-line sequence (plus an optional DESIGN_REVIEW node).
    """
    phases: list[PhaseSpec] = [
        PhaseSpec(
            name="PLAN",
            handler_key="plan",
            depends_on=(),
            max_retries=0,
        ),
        PhaseSpec(
            name="RESEARCH",
            handler_key="research",
            depends_on=("PLAN",),
            max_retries=0,
        ),
        PhaseSpec(
            name="SYNTHESIZE",
            handler_key="synthesize",
            depends_on=("RESEARCH",),
            max_retries=0,
        ),
        PhaseSpec(
            name="IMPLEMENT",
            handler_key="implement",
            depends_on=("SYNTHESIZE",),
            max_retries=1,  # the pre-extraction runner does at most 1 re-attempt
        ),
        PhaseSpec(
            name="VERIFY",
            handler_key="verify",
            depends_on=("IMPLEMENT",),
            max_retries=max_verify_retries,
        ),
    ]

    if design_review_enabled:
        phases.append(
            PhaseSpec(
                name="DESIGN_REVIEW",
                handler_key="design_review",
                depends_on=("VERIFY",),
                max_retries=0,
                condition=make_design_review_condition(design_visual_surface_globs),
            )
        )
        review_depends_on: tuple[str, ...] = ("DESIGN_REVIEW",)
    else:
        review_depends_on = ("VERIFY",)

    phases += [
        PhaseSpec(
            name="REVIEW",
            handler_key="review",
            depends_on=review_depends_on,
            max_retries=0,
        ),
        PhaseSpec(
            name="TECH_LEAD",
            handler_key="tech_lead",
            depends_on=("REVIEW",),
            max_retries=0,
            condition=_tech_lead_condition,
        ),
        PhaseSpec(
            name="OPEN_PR",
            handler_key="open_pr",
            depends_on=("REVIEW",),
            max_retries=0,
        ),
    ]

    return PhaseGraph(phases)
