"""Post-gate routing policy: what happens to a PR after the gate rules on it.

This is the decision the bash reconciler (``ops/vps/orchestrator/fleet_reconcile.sh``)
made with two EREs and a per-head counter file. It is pure here, so the whole
table is testable without a repository, a network, a model, or a running gate:

    classify(line) -> Verdict                      # which marker the verdict is
    decide(lines, head=..., lane=..., counters=...) -> Decision

Everything is a value. IO — reading the status file, reading and appending the
attempt counters, running the action — lives in :mod:`agent_fleet.routing.counters`
and :mod:`agent_fleet.routing.executor`.

The verdict vocabulary is the status-file contract from
:mod:`agent_fleet.fleet_ops.statusfile`: one line per gate run, appended, and
``HH:MM:SS NEEDS-ESCALATION <reason>`` where the reason carries the marker.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import Any

from agent_fleet.fleet_ops.statusfile import APPROVED_TOKEN, ESCALATION_TOKEN

#: Automatic re-gates allowed per PR head. Three is what the reconciler allowed
#: before a head that keeps failing on infrastructure is worth a human's time.
MAX_REGATE_PER_HEAD = 3
#: Reworks allowed per *lane* (all heads). A converging lane gets a few rounds;
#: one that is not converging is parked below, so this is a backstop for the
#: rounds where the counts did look like they were dropping.
MAX_REWORK_PER_LANE = 3
#: A rebase, a repair, or a no-push rework happens at most once per head. These
#: are the actions that rewrite the PR, so a second one on the same head means
#: the first did not work and doing it again is not the answer.
ONE_PER_HEAD = 1


class Action(enum.StrEnum):
    """The one thing to do with a PR next."""

    #: The gate approved this head. Merge it.
    MERGE = "merge"
    #: The gate could not run: infrastructure died, or the PR is untestable as
    #: it stands and the base has not been merged in yet.
    REGATE = "regate"
    #: The merged-tree regression check failed — base is in and the PR still
    #: conflicts or regresses against it.
    REBASE = "rebase"
    #: The PR's own tests cannot run (no INFRA cause) or broke after a fix
    #: round. Someone has to make them runnable again.
    REPAIR = "repair"
    #: Spend another agent round: the fix loop is converging, or the gate fixed
    #: nothing and one fresh full pass is worth it.
    REWORK = "rework"
    #: Do nothing. Stop spending agents on this head and let a human look.
    PARK = "park"


class Verdict(enum.StrEnum):
    """Which marker the gate's last verdict carries, before any cap applies."""

    APPROVED = "approved"
    INFRA = "infra"
    MERGED_TREE = "merged-tree"
    UNRUNNABLE = "unrunnable"
    BROKEN_TESTS = "broken-tests"
    ROUND_LOOP = "round-loop"
    NO_PUSH = "no-push"
    UNTESTABLE = "untestable"
    #: No terminal verdict, or an escalation whose reason carries no marker this
    #: module knows. Both are parked: an unknown verdict is not a licence to
    #: spend another agent round guessing what it meant.
    UNKNOWN = "unknown"


#: Line *shape*: an optional ``HH:MM:SS`` clock, then the verdict field, then the
#: rest. Anchored on purpose. The escalation line carries a tail of the agent's
#: own final message, which is model-authored: an unanchored search for a token
#: anywhere in the line lets a model quote ``PREMERGE-APPROVED`` into its summary
#: and have the line classified as an approval. The verdict is the first field
#: after the clock, so that is the only place it is read.
_VERDICT_FIELD_RE = re.compile(r"^(?:\d{2}:\d{2}:\d{2}\s+)?(?P<token>\S+)(?:\s+(?P<detail>.*))?$")

_APPROVED_RE = re.compile(rf"{APPROVED_TOKEN}\s+(?P<sha>[0-9a-f]{{7,40}})", re.IGNORECASE)

#: Infrastructure the gate could not get past. None of these is a statement
#: about the PR's quality, which is why they buy a fresh gate run rather than a
#: rewrite.
_INFRA_RE = re.compile(
    r"fail-closed"
    # Three spellings are in status files on disk: `agent(s) died` (the gate's
    # own fail-closed reason), `agent cursor died (exit=1)` (the older form), and
    # a bare `agent died`. The optional group covers all three.
    r"|agent(?:\(s\)| \S+)? died"
    r"|github unreachable"
    r"|gate refused:"
    r"|cannot create (?:gate|fix|recheck) worktree"
    # The INFRA marker is in the *payload* here, not the verdict: the pytest
    # layer prints `INFRA <pkgdir>: <error>` and the gate interpolates it into
    # the escalation. So the last alternative carries its own lookahead — the
    # same line without INFRA is an untestable PR, which is a different action.
    r"|gate tests could not run at head .*\bINFRA\b",
    re.IGNORECASE,
)

_MERGED_TREE_RE = re.compile(r"merged-tree regression check failed", re.IGNORECASE)

#: The *same* line as the last :data:`_INFRA_RE` alternative with the INFRA
#: marker absent: the gate ran, the tests did not, and nothing about the machine
#: stopped them. That is a PR that needs its tests made runnable. The python
#: pipeline spells this two ways — ``tests could not run on the rebased head:
#: pytest could not run in <pkg> (exit 2)`` and a bare ``pytest could not run in
#: <pkg> (exit 2)`` — and both carry the same meaning as the bash
#: ``gate tests could not run at head`` form, so all three are UNRUNNABLE.
_UNRUNNABLE_RE = re.compile(
    r"(?:gate tests could not run at head"
    r"|tests could not run on the rebased head"
    r"|pytest could not run in \S+ \(exit \d+\))(?!.*\bINFRA\b)",
    re.IGNORECASE,
)

#: The outcome-named fix-loop reasons. The leading ``(`` is optional because the
#: two gate implementations wrap the escalation differently: the bash driver
#: parenthesises the whole reason, while the python pipeline appends it bare
#: (``f"{stamp} NEEDS-ESCALATION {reason}"`` with
#: ``reason = f"{outcome} after {n} round(s); failing by round: ..."``). Requiring
#: the parenthesis read every python-gate verdict as UNKNOWN, so a converging
#: lane never got its rework round. The optional group accepts both spellings.
_BROKEN_TESTS_RE = re.compile(r"\(?\s*tests-broken after", re.IGNORECASE)
_ROUND_LOOP_RE = re.compile(r"\(?\s*(?:stalled|cap) after \d+ round\(s\)", re.IGNORECASE)
_NO_PUSH_RE = re.compile(r"\(?\s*no-push after", re.IGNORECASE)
_UNTESTABLE_RE = re.compile(r"\(?\s*untestable-unresolved after", re.IGNORECASE)

#: The escalation markers, in the order they are checked. First match wins.
#: The order is the spec's, and each row is also independently correct: the one
#: pair that can match the same line (INFRA vs UNRUNNABLE, both keyed on
#: ``gate tests could not run at head``) resolves on the payload's own lookahead
#: rather than on which row comes first, so a row still classifies its own line
#: correctly when tested alone.
_ESCALATION_TABLE: tuple[tuple[Verdict, re.Pattern[str]], ...] = (
    (Verdict.INFRA, _INFRA_RE),
    (Verdict.MERGED_TREE, _MERGED_TREE_RE),
    (Verdict.UNRUNNABLE, _UNRUNNABLE_RE),
    (Verdict.BROKEN_TESTS, _BROKEN_TESTS_RE),
    (Verdict.ROUND_LOOP, _ROUND_LOOP_RE),
    (Verdict.NO_PUSH, _NO_PUSH_RE),
    (Verdict.UNTESTABLE, _UNTESTABLE_RE),
)

#: ``failing by round: 5 3 2; see /g/rounds.tsv`` — the per-round failing-test
#: counts. The list stops at the first ``;`` or ``)``, which is where both gate
#: implementations end it (the bash driver follows the counts with ``; see
#: <file>`` and wraps the whole escalation in parentheses). Without that bound
#: the trailing ``2)`` is not a digit and the last round's count is silently
#: dropped — which is the count the convergence test actually compares, so a
#: dropping bug here is a convergence decision that flips.
_COUNTS_RE = re.compile(r"failing by round:([^;)]*)")

#: Shortest sha prefix that can be compared between two lines of a status file.
#: Matches the gate's own minimum, so a truncated sha is compared as far as it
#: is trustworthy rather than being treated as a mismatch.
SHA_MIN_CHARS = 7


def _split_verdict(line: str) -> tuple[str, str]:
    """``"12:00:00 NEEDS-ESCALATION foo: bar"`` -> ``("NEEDS-ESCALATION", "foo: bar")``."""
    match = _VERDICT_FIELD_RE.match((line or "").strip())
    if match is None:
        return "", ""
    return match.group("token"), (match.group("detail") or "").strip()


def is_terminal(line: str) -> bool:
    """Whether *line* carries a terminal verdict field (approval or escalation)."""
    return _split_verdict(line)[0] in (APPROVED_TOKEN, ESCALATION_TOKEN)


def last_verdict_line(lines: tuple[str, ...]) -> str:
    """The most recent terminal verdict in *lines*, or ``""``.

    Last-wins, the same rule the automerge applies: a later escalation
    supersedes an earlier approval, so a PR that was approved and then failed a
    re-gate is not merged on the strength of the first verdict.
    """
    for line in reversed(lines):
        if is_terminal(line):
            return line
    return ""


def _same_sha(left: str, right: str) -> bool:
    """Whether two shas agree on the characters both of them actually carry."""
    width = min(len(left), len(right), SHA_MIN_CHARS)
    return width >= SHA_MIN_CHARS and left[:width].lower() == right[:width].lower()


def approved_sha(line: str) -> str:
    """The sha an approval line approves, or ``""``."""
    match = _APPROVED_RE.search(line or "")
    return match.group("sha") if match else ""


def round_counts(detail: str) -> list[int]:
    """The per-round failing-test counts from a fix-loop escalation, oldest first.

    Both separators the two gate implementations use are accepted (space-joined
    by the bash driver, comma-joined by the python pipeline). Unparseable input
    yields ``[]`` rather than a guess, and the caller parks on an empty list, so
    a malformed tail can never be read as convergence.
    """
    match = _COUNTS_RE.search(detail or "")
    if match is None:
        return []
    counts: list[int] = []
    for token in re.split(r"[,\s]+", match.group(1)):
        if token.isdigit():
            counts.append(int(token))
    return counts


def is_converging(counts: list[int]) -> bool:
    """Whether the failing-test count is falling across the rounds.

    Two rounds or more, and the last is strictly below the first. One round is
    not convergence — there is no trend in a single point — and neither is a
    flat or rising tail, so both park.
    """
    return len(counts) >= 2 and counts[-1] < counts[0]


def classify(line: str) -> tuple[Verdict, str]:
    """Which marker *line* carries, and the detail field it was read from.

    Pure and total: any line in, one of :class:`Verdict` out. The detail is
    returned so the decision can quote the reason it acted on.
    """
    token, detail = _split_verdict(line)
    if token == APPROVED_TOKEN:
        return Verdict.APPROVED, detail
    if token != ESCALATION_TOKEN:
        return Verdict.UNKNOWN, detail
    for verdict, pattern in _ESCALATION_TABLE:
        if pattern.search(detail):
            return verdict, detail
    return Verdict.UNKNOWN, detail


@dataclass(frozen=True)
class RouteCounters:
    """How much of each action's budget this head and lane have already spent.

    The counts are inputs, not a lookup: the policy does not decide *whether* to
    count an attempt, only what the count means. Counting is the caller's job
    (:func:`agent_fleet.routing.counters.record_attempt`), because it is the
    attempt that must be charged, not the decision that asked for one.
    """

    #: re-gates on this head (budget :data:`MAX_REGATE_PER_HEAD`)
    regate_at_head: int = 0
    #: rewrites on this head, per action
    rebase_at_head: int = 0
    repair_at_head: int = 0
    #: no-push / untestable rewrites on this head (budget :data:`ONE_PER_HEAD`)
    rework_at_head: int = 0
    #: converging-lane rewrites across every head of this lane
    #: (budget :data:`MAX_REWORK_PER_LANE`)
    rework_at_lane: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "regate_at_head": self.regate_at_head,
            "rebase_at_head": self.rebase_at_head,
            "repair_at_head": self.repair_at_head,
            "rework_at_head": self.rework_at_head,
            "rework_at_lane": self.rework_at_lane,
        }


@dataclass(frozen=True)
class Decision:
    """The policy's answer: one action, the verdict it came from, and why."""

    action: Action
    verdict: Verdict
    reason: str
    #: True when the action would have applied but its budget was already spent.
    #: The action is then :attr:`Action.PARK`: an exhausted budget means more
    #: agents on this head will not help, which is a human's call to make.
    exhausted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "verdict": self.verdict.value,
            "reason": self.reason,
            "exhausted": self.exhausted,
        }


def _budgeted(
    action: Action,
    used: int,
    budget: int,
    verdict: Verdict,
    detail: str,
    what: str,
) -> Decision:
    """Apply one action's budget, spending it or parking.

    *what* names the budget in the reason, so an operator reading the log sees
    which of the five counters stopped the work rather than a bare "park".
    """
    if used >= budget:
        return Decision(
            action=Action.PARK,
            verdict=verdict,
            reason=f"{what}: {used}/{budget} already used; {detail}".strip(" ;"),
            exhausted=True,
        )
    return Decision(
        action=action,
        verdict=verdict,
        reason=f"{detail}; {what} {used + 1}/{budget}".strip(" ;"),
    )


def decide(
    lines: tuple[str, ...],
    *,
    head: str = "",
    lane: str = "",
    counters: RouteCounters | None = None,
) -> Decision:
    """Decide the one action to take on a PR, from its gate verdict history.

    The rules, in the order they are checked, and why in that order:

    1. **Approved** -> merge. A gate approval is the only verdict that ends the
       lane, so it is checked first. If *head* is given and the approval names a
       different sha, the head moved after the gate approved, and the approval
       is stale: re-gate. Merging a head the gate never saw is the one thing
       this policy must never do.
    2. **Infrastructure failure** -> regate. The gate never got an answer, so
       it has said nothing about the PR.
    3. **Merged-tree regression** -> rebase. Base is in and the PR conflicts or
       regresses against it; the PR is fine, the base moved under it.
    4. **Tests will not run, or broke after a round** -> repair. Real, and not
       fixable by re-running the gate.
    5. **Stalled or capped, counts falling** -> rework. The fix loop is making
       progress. **Counts flat or rising** -> park: more rounds on a lane that
       is not converging is how a lane burns its budget.
    6. **No-push or untestable-unresolved** -> rework once, then park.

    Every rule except 1 is budgeted, and a spent budget parks rather than
    repeating. *lane* is carried for the log and the per-lane counter; the pure
    function needs nothing else.
    """
    counts = counters or RouteCounters()
    line = last_verdict_line(lines)
    verdict, detail = classify(line)

    if verdict is Verdict.APPROVED:
        approved = approved_sha(line)
        if head and approved and not _same_sha(approved, head):
            return _budgeted(
                Action.REGATE,
                counts.regate_at_head,
                MAX_REGATE_PER_HEAD,
                verdict,
                f"approved {approved} but head is {head[:9]} — approval is stale",
                "re-gate",
            )
        return Decision(Action.MERGE, verdict, f"gate approved {approved or 'the head'}")

    if verdict is Verdict.INFRA:
        return _budgeted(
            Action.REGATE,
            counts.regate_at_head,
            MAX_REGATE_PER_HEAD,
            verdict,
            f"gate could not run: {detail}",
            "re-gate",
        )

    if verdict is Verdict.MERGED_TREE:
        return _budgeted(
            Action.REBASE,
            counts.rebase_at_head,
            ONE_PER_HEAD,
            verdict,
            f"base is merged in and the PR regressed against it: {detail}",
            "rebase",
        )

    if verdict in (Verdict.UNRUNNABLE, Verdict.BROKEN_TESTS):
        return _budgeted(
            Action.REPAIR,
            counts.repair_at_head,
            ONE_PER_HEAD,
            verdict,
            f"the PR's own tests do not run: {detail}",
            "repair",
        )

    if verdict is Verdict.ROUND_LOOP:
        rounds = round_counts(detail)
        if not is_converging(rounds):
            shown = " ".join(str(n) for n in rounds) or "no counts"
            return Decision(
                Action.PARK,
                verdict,
                f"fix loop is not converging (failing by round: {shown})",
            )
        return _budgeted(
            Action.REWORK,
            counts.rework_at_lane,
            MAX_REWORK_PER_LANE,
            verdict,
            f"fix loop is converging (failing by round: {' '.join(map(str, rounds))})",
            f"rework for {lane or 'this lane'}",
        )

    if verdict in (Verdict.NO_PUSH, Verdict.UNTESTABLE):
        # One budget for both: no-push and untestable-unresolved are the same
        # failure seen from two sides — the fix round produced no pushable
        # change — and spending one to have the other still owed a round is
        # how a lane gets twice the work for one problem.
        return _budgeted(
            Action.REWORK,
            counts.rework_at_head,
            ONE_PER_HEAD,
            verdict,
            f"the fix loop achieved nothing: {detail}",
            "rework",
        )

    if not line:
        return Decision(Action.PARK, verdict, "no terminal gate verdict yet")
    return Decision(Action.PARK, verdict, f"unrecognised gate verdict: {detail}")
