"""The STANDARD bar: risk-matched review for a PR that touches nothing sensitive.

The full evidence gate spends four reviewers, a verifier per claim, a judge and
a convergence loop to answer "may this merge". For most PRs that is the wrong
amount of machine: a well-scoped change to ordinary product code, reviewed once
by someone who covers every focus, is not thinner evidence — it is the same
question asked once instead of four times in parallel.

What buys the confidence is the *deterministic* half, which the full gate also
runs and which the standard bar leans on harder:

- the PR's own changed tests are run at head (step0) before anything else, and a
    red test is a blocker regardless of what the reviewer says;
- when the reviewer does report something, exactly one fixer is dispatched with
    the findings and the failing tests, and it pushes to the PR's own head ref;
- the run ends ``re-gate new head``: the fixer's new head is not trusted, the
    gate re-runs there from scratch;
- if the fixer changed nothing (every finding disputed), or the PR burns its
    pass budget, the bar *falls back to the full evidence gate* for that head.

The two ways out of the cheap bar are the interesting part. A pass counter alone
is a treadmill — a fixer that keeps finding new things to fix would loop
forever — so the bar is finite: at most ``standard_max_passes`` passes (default
3) and then the full gate reads the head. And a fixer that disputes every
finding and changes nothing is not making progress, so that head is escalated to
the full gate immediately rather than spending a pass on it.

Tier selection itself is a pure function of the diff, so the decision this module
records can be tested without a repository, a network, or a model:

    select_tier(config, changed) -> "standard" | "sensitive"

SENSITIVE is today's pipeline, unchanged. A PR that touches no sensitive path is
STANDARD. Everything else — reviewer count, the state machine's inputs and
outputs — is a value here, so the state machine can be exercised directly:

    next_action(StandardState(...)) -> StandardAction
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_fleet.gate.config import GateConfig

#: Tier name for the light bar. Kept as a string rather than an enum member so it
#: never collides with the review-tier *count* the full gate logs: that number is
#: how many reviewers ran, and STANDARD is one reviewer under a different rule
#: about what happens after, not a different count.
STANDARD_TIER = "standard"
#: Tier name for today's full evidence pipeline, kept unchanged by this lane.
SENSITIVE_TIER = "sensitive"

#: Terminal outcomes this bar records on a metrics row. They are strings in the
#: same vocabulary as the full gate's ``outcome`` so one metrics table reads
#: both, and they are what :func:`prior_passes` counts.
OUTCOME_FIX_AND_REGATE = "fix-and-regate"
OUTCOME_APPROVED = "standard-approved"
OUTCOME_FALLBACK = "standard-fallback"

#: How many metrics rows the pass counter reads. The counter needs this PR's own
#: rows and nothing else, but the metrics file is shared by every run on the host
#: and is never rotated, so the read is a bounded tail: a window this wide cannot
#: plausibly be filled by other PRs' runs between two passes of one PR, which is
#: what keeps the cheap bar's bookkeeping from costing more than the bar does.
STANDARD_HISTORY_ROWS = 500


def select_tier(config: GateConfig, changed: list[str]) -> str:
    """Return the tier *changed* earns: ``standard`` unless it is sensitive.

    A single sensitive path is a veto regardless of the rest of the diff. The
    point of the pattern list is that a wrong verdict on any one of these paths
    is not recoverable by re-gating, so the cheap bar is offered only when the
    *whole* diff is outside the set.
    """
    if config.sensitive_paths_in(changed):
        return SENSITIVE_TIER
    return STANDARD_TIER


class StandardAction(enum.StrEnum):
    """What the STANDARD bar does next, given the state it is handed."""

    #: The single all-focus reviewer reported nothing and the PR's tests are
    #: green: nothing left to weigh, so approve.
    APPROVE = "approve"
    #: Hand the findings and the failing tests to one fixer. Costs a pass; the
    #: pipeline then re-gates the head the fixer pushed, or falls back if it
    #: pushed nothing.
    FIX_AND_REGATE = "fix-and-regate"
    #: The bar has spent its budget (or the fixer changed nothing): stop paying
    #: for the cheap bar and read this head with the full evidence gate.
    FALLBACK = "fallback"


class FallbackReason(enum.StrEnum):
    """Why the cheap bar gave up on a head, so the log can say which."""

    #: Every finding was disputed and the fixer changed nothing.
    DISPUTED = "disputed"
    #: ``standard_max_passes`` fixer passes have been spent on this head.
    PASS_BUDGET = "pass-budget"


@dataclass(frozen=True)
class StandardState:
    """Everything the STANDARD decision depends on, and nothing that is not it.

    A value, not the pipeline: the pipeline reads its evidence and builds one of
    these, and the state machine answers from it alone. That is what makes the
    pass/fallback rules testable here rather than only through a live gate run.
    """

    #: Which tier the diff earned. SENSITIVE never reaches this state machine; it
    #: is carried so a decision can be logged with the tier that produced it.
    tier: str = STANDARD_TIER
    #: Confirmations from the single reviewer plus step0's failing PR tests.
    findings: int = 0
    #: Whether the PR's own changed tests were red at head.
    pr_tests_failed: bool = False
    #: Fixer passes already spent on this head.
    passes: int = 0
    #: The last fixer reported every finding disputed and changed nothing.
    fixer_changed_nothing: bool = False
    #: Cap on fixer passes before the bar falls back.
    max_passes: int = 3
    #: The head the decision applies to, for the log line and the reason.
    head: str = ""

    @property
    def blocked(self) -> bool:
        """Whether there is anything for a fixer to do at all."""
        return bool(self.findings) or self.pr_tests_failed

    @property
    def out_of_passes(self) -> bool:
        """Whether this head has spent the pass budget."""
        return self.passes >= self.max_passes


@dataclass(frozen=True)
class StandardDecision:
    """The bar's answer: the action, and the pass count it was decided at."""

    action: StandardAction
    passes: int
    fallback_reason: FallbackReason | None = None
    #: One line for ``gate.tier``/``gate.outcome``, so a run's bar is readable
    #: after the fact without re-deriving the diff.
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "passes": self.passes,
            "fallback_reason": self.fallback_reason.value if self.fallback_reason else None,
            "summary": self.summary,
        }


def next_action(state: StandardState) -> StandardDecision:
    """Decide what the STANDARD bar does with *state*.

    The rules, in the order they are checked, and why in that order:

    1. **Nothing blocked** → approve. Zero findings and green PR tests is the
       only clean exit, and it must be checked first: a PR that needed no
       fixer should never be charged a pass or a fallback.
    2. **Fixer changed nothing** → fall back. Every finding disputed and no diff
       is the fixer declining the work, not fixing it. Re-gating that head under
       the same cheap bar would just produce the same refusal, so the full
       evidence gate reads it instead.
    3. **Out of passes** → fall back. The counter exists so the cheap bar is
       finite; once the head has spent it, the evidence is the accumulated
       passes, not another pass.
    4. Otherwise → one fixer pass, then re-gate the new head.

    A pass is only ever spent on a fixer that actually ran and was expected to
    change something, so the budget bounds real work, not retries.
    """
    head_note = f" at {state.head[:9]}" if state.head else ""

    if not state.blocked:
        return StandardDecision(
            action=StandardAction.APPROVE,
            passes=state.passes,
            summary=(
                f"standard bar: no findings and PR tests green{head_note} (pass {state.passes})"
            ),
        )

    if state.fixer_changed_nothing:
        return StandardDecision(
            action=StandardAction.FALLBACK,
            passes=state.passes,
            fallback_reason=FallbackReason.DISPUTED,
            summary=(
                f"standard bar: fixer changed nothing (all findings disputed){head_note}; "
                "falling back to the full evidence gate"
            ),
        )

    if state.out_of_passes:
        return StandardDecision(
            action=StandardAction.FALLBACK,
            passes=state.passes,
            fallback_reason=FallbackReason.PASS_BUDGET,
            summary=(
                f"standard bar: pass budget spent ({state.passes} of {state.max_passes})"
                f"{head_note}; falling back to the full evidence gate"
            ),
        )

    return StandardDecision(
        action=StandardAction.FIX_AND_REGATE,
        passes=state.passes + 1,
        summary=(
            f"standard bar: {state.findings} finding(s)"
            f"{', PR tests red' if state.pr_tests_failed else ''}{head_note}; "
            f"one fixer pass {state.passes + 1} of {state.max_passes}, then re-gate the new head"
        ),
    )


def approval_reason(state: StandardState) -> str:
    """The operator-facing reason for a STANDARD approval.

    The status line is what the automerge reads, so the approval says which bar
    approved and on what evidence — a cheap-bar approval is a different claim
    from a full-gate one and must not be indistinguishable after the fact.
    """
    return (
        f"standard bar approved: no findings and PR tests green "
        f"(pass {state.passes}, {state.max_passes} allowed)"
    )


def _as_int(value: object) -> int:
    """Coerce an untrusted on-disk value to int; bad input reads as 0."""
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


def prior_passes(rows: list[dict[str, Any]], *, repo: str, pr: int, max_passes: int = 3) -> int:
    """How many STANDARD fixer passes this PR has already spent, from metrics rows.

    The gate process exits after one run, so the pass counter has to survive it:
    each run re-reads its own history and either spends another pass or falls
    back. Counting is per ``(repo, pr)`` and only counts rows this bar wrote —
    a full-gate run for the same PR is not a STANDARD pass and must not inflate
    the count. Rows are newest-last, so the walk goes backwards over the rows
    this PR's STANDARD bar produced:

    - a ``fix-and-regate`` row is a spent pass: count it and keep walking;
    - a ``fallback`` row closes the budget for good. Rows are walked newest-first,
      so the fallback is seen *before* the passes it terminated, and returning
      there — or breaking out having counted nothing — would report the passes as
      zero and hand the next head a fresh budget, buying one more fixer pass per
      new head on a PR the cheap bar already refused. The count is therefore
      saturated to the budget rather than discarded: a fallback means the budget
      is spent, whatever the arithmetic under it, which is what makes the refusal
      durable instead of advisory;
    - an ``approved`` row is not a pass and does not stop the walk — the PR was
      approved, and if a new head arrives the budget reasoning starts from the
      passes that were actually spent;
    - a non-STANDARD tier closes the budget outright — the full pipeline read
      this PR, so the cheap bar is done with it.

    Saturating to *max_passes* rather than counting the passes beneath the
    fallback is what covers the disputed refusal too: a fixer that changed
    nothing on pass one spent nothing, and only a spent budget stops the next
    head from buying the same pass again.
    """
    spent = 0
    for row in reversed(rows):
        if str(row.get("repo", "")) != repo or _as_int(row.get("pr")) != pr:
            continue
        if str(row.get("tier", "")) != STANDARD_TIER:
            break
        outcome = str(row.get("outcome", ""))
        if outcome == OUTCOME_FALLBACK:
            return max(spent, max_passes)
        if outcome == OUTCOME_FIX_AND_REGATE:
            spent += 1
    return spent
