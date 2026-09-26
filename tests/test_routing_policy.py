"""The post-gate routing policy table, row by row.

Every rule the bash reconciler expressed as two EREs plus a counter file is a
row here, and each row is tested at the decision level — the whole table is a
pure function of the verdict history, so none of it needs a repository, a
network, a model, or a running gate.

The cases are grouped by *rule*, and within a rule by the branch that rule takes,
because the thing worth pinning is not "does the code run" but "does this
marker, and only this marker, reach this action". The two rows that can both
match one line — a test run that could not start because of INFRA versus one that
could not start because the PR is untestable — are tested in both directions, so
neither can be satisfied by accident through table order.
"""

from __future__ import annotations

import pytest

from agent_fleet.routing.policy import (
    MAX_REGATE_PER_HEAD,
    MAX_REWORK_PER_LANE,
    Action,
    Decision,
    RouteCounters,
    Verdict,
    approved_sha,
    classify,
    decide,
    is_converging,
    is_terminal,
    last_verdict_line,
    round_counts,
)

HEAD = "0dc2391ab3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8"


def line(detail: str, *, token: str = "NEEDS-ESCALATION", clock: str = "10:00:00") -> str:
    return f"{clock} {token} {detail}"


def decide_one(detail: str, *, counters: RouteCounters | None = None) -> Decision:
    return decide((line(detail),), counters=counters)


def decide_late(
    detail: str,
    *,
    token: str = "NEEDS-ESCALATION",
    head: str = "",
    counters: RouteCounters | None = None,
) -> Decision:
    """A verdict behind the gate's progress chatter, as a real status file has it."""
    lines = (
        "09:00:00 start @0dc2391ab (PR #3544)",
        "09:10:00 find: 4 candidate blockers from 5 lens(es)",
        "09:20:00 round 1 @0dc2391ab: failing 4 -> 2 (fixed 3, new 1)",
        line(detail, token=token),
    )
    return decide(lines, head=head, counters=counters)


# ---------------------------------------------------------------------------
# Rule 1: PREMERGE-APPROVED -> merge
# ---------------------------------------------------------------------------


def test_approved_merges() -> None:
    decision = decide((line("0dc2391ab", token="PREMERGE-APPROVED"),), head=HEAD)
    assert decision.action is Action.MERGE
    assert decision.verdict is Verdict.APPROVED
    assert not decision.exhausted


def test_approved_on_this_head_merges() -> None:
    assert decide_late("0dc2391ab", token="PREMERGE-APPROVED", head=HEAD).action is Action.MERGE


def test_approval_for_a_different_head_is_stale_and_regates() -> None:
    """The head moved after the gate approved; merging it would merge unapproved code."""
    decision = decide((line("aaaaaaaaa", token="PREMERGE-APPROVED"),), head=HEAD)
    assert decision.action is Action.REGATE
    assert "stale" in decision.reason


def test_approval_matching_on_a_short_prefix_still_merges() -> None:
    """The status line carries a sha9; comparing it to a full sha must not fail."""
    assert decide((line("0dc2391ab", token="PREMERGE-APPROVED"),), head=HEAD).action is (
        Action.MERGE
    )


def test_approval_superseded_by_a_later_escalation() -> None:
    """Last verdict wins: an approval followed by a failure must not merge."""
    lines = (
        line("0dc2391ab", token="PREMERGE-APPROVED", clock="09:00:00"),
        line("merged-tree regression check failed at 0dc2391ab", clock="10:00:00"),
    )
    assert decide(lines, head=HEAD).action is Action.REBASE


# ---------------------------------------------------------------------------
# Rule 2: infra failures -> regate, max 3 per head
# ---------------------------------------------------------------------------

INFRA_REASONS = [
    "fail-closed: agent(s) died or returned no result after 1 retry: DEAD lens",
    "fail-closed: gate stuck 6000s without progress; stopped by reconcile for re-gate",
    "infra: github unreachable (empty gh answer after 5 tries); retryable",
    "gate refused: owner/repo#1234 is 'somewhere' (CLOSED), expected open fb/lane",
    "cannot create gate worktree",
    "cannot create fix worktree (base main)",
    "cannot create recheck worktree (base main)",
    "agent cursor died (exit=1)",
    "gate tests could not run at head (INFRA tests: rc=2 ImportError)",
]


@pytest.mark.parametrize("reason", INFRA_REASONS)
def test_infra_reasons_regate(reason: str) -> None:
    decision = decide_late(reason)
    assert decision.verdict is Verdict.INFRA
    assert decision.action is Action.REGATE


@pytest.mark.parametrize("count", range(MAX_REGATE_PER_HEAD))
def test_regate_allowed_up_to_the_cap(count: int) -> None:
    decision = decide_late(
        "fail-closed: agent(s) died", counters=RouteCounters(regate_at_head=count)
    )
    assert decision.action is Action.REGATE
    assert not decision.exhausted


def test_regate_past_the_cap_parks_and_says_the_budget_was_spent() -> None:
    decision = decide_late(
        "fail-closed: agent(s) died",
        counters=RouteCounters(regate_at_head=MAX_REGATE_PER_HEAD),
    )
    assert decision.action is Action.PARK
    assert decision.exhausted
    assert "3/3" in decision.reason


def test_regate_budget_is_per_head_not_per_lane() -> None:
    """The budget is keyed on the head, so a different head of the same lane is fresh.

    The *rework* budget is the one that spans a lane, because its failure mode
    is a lane that keeps looking promising; an infra re-gate cap exists to stop
    retrying one broken head, which a new push has already changed.
    """
    decision = decide_late("fail-closed: agent(s) died", counters=RouteCounters(regate_at_head=3))
    assert decision.action is Action.PARK  # same head, budget spent
    fresh = decide_late(
        "fail-closed: agent(s) died",
        counters=RouteCounters(regate_at_head=3, rework_at_lane=MAX_REWORK_PER_LANE),
    )
    assert fresh.action is Action.PARK
    assert fresh.reason.startswith("re-gate:")


# ---------------------------------------------------------------------------
# Rule 3: merged-tree regression -> rebase, once per head
# ---------------------------------------------------------------------------


def test_merged_tree_regression_rebases() -> None:
    decision = decide_late("merged-tree regression check failed at 0dc2391ab (see g/x.log)")
    assert decision.verdict is Verdict.MERGED_TREE
    assert decision.action is Action.REBASE


def test_rebase_is_allowed_once_per_head() -> None:
    assert (
        decide_late(
            "merged-tree regression check failed", counters=RouteCounters(rebase_at_head=0)
        ).action
        is Action.REBASE
    )


def test_rebase_twice_parks() -> None:
    decision = decide_late(
        "merged-tree regression check failed", counters=RouteCounters(rebase_at_head=1)
    )
    assert decision.action is Action.PARK
    assert decision.exhausted


# ---------------------------------------------------------------------------
# Rule 4: tests will not run / broke after a round -> repair, once per head
# ---------------------------------------------------------------------------

REPAIR_REASONS = [
    "gate tests could not run at head (SUMMARY tests: rc=2 3 errors during collection)",
    "(tests-broken after 1 round(s); failing by round: 3 3; see /g/rounds.tsv)",
    "(tests-broken after 2 round(s); failing by round: 5 4; see /g/rounds.tsv)",
]


@pytest.mark.parametrize("reason", REPAIR_REASONS)
def test_unrunnable_reasons_repair(reason: str) -> None:
    decision = decide_late(reason)
    assert decision.verdict in (Verdict.UNRUNNABLE, Verdict.BROKEN_TESTS)
    assert decision.action is Action.REPAIR


def test_the_same_line_with_infra_is_infra_not_repair() -> None:
    """The one line two rules can match.

    ``gate tests could not run at head`` covers both "the machine broke" (INFRA
    in the payload) and "this PR cannot be tested". The payload decides, not the
    order the table is checked in.
    """
    without = "gate tests could not run at head (SUMMARY tests: rc=2)"
    with_infra = "gate tests could not run at head (INFRA tests: rc=2 ImportError)"
    assert classify(line(without))[0] is Verdict.UNRUNNABLE
    assert classify(line(with_infra))[0] is Verdict.INFRA
    assert decide_late(without).action is Action.REPAIR
    assert decide_late(with_infra).action is Action.REGATE


def test_repair_is_allowed_once_per_head() -> None:
    assert (
        decide_late(
            "(tests-broken after 1 round(s); failing by round: 3 3)", counters=RouteCounters()
        ).action
        is Action.REPAIR
    )


def test_repair_twice_parks() -> None:
    decision = decide_late(
        "(tests-broken after 1 round(s); failing by round: 3 3)",
        counters=RouteCounters(repair_at_head=1),
    )
    assert decision.action is Action.PARK
    assert decision.exhausted


# ---------------------------------------------------------------------------
# Rule 5: stalled/capped -> rework when converging, else park
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("marker", ["stalled", "cap"])
def test_converging_counts_rework(marker: str) -> None:
    decision = decide_late(f"({marker} after 3 round(s); failing by round: 5 4 2)")
    assert decision.verdict is Verdict.ROUND_LOOP
    assert decision.action is Action.REWORK


@pytest.mark.parametrize(
    "counts",
    [
        "2 3 3",  # flat tail
        "2 3 4",  # rising
        "4 4",  # flat throughout
        "3 1 5",  # ends worse than it started
    ],
)
def test_non_converging_counts_park(counts: str) -> None:
    decision = decide_late(f"(stalled after 3 round(s); failing by round: {counts})")
    assert decision.action is Action.PARK
    assert not decision.exhausted  # parked on the evidence, not on a spent budget


def test_a_single_round_is_not_convergence() -> None:
    """One point has no trend; reading it as convergence buys rounds for nothing."""
    assert decide_late("(stalled after 1 round(s); failing by round: 9)").action is Action.PARK


def test_missing_counts_park_rather_than_assume_convergence() -> None:
    decision = decide_late("(stalled after 3 round(s); see /g/rounds.tsv)")
    assert decision.action is Action.PARK


def test_comma_separated_counts_are_understood() -> None:
    """The python pipeline joins counts with commas; the bash gate with spaces."""
    assert decide_late("(cap after 3 round(s); failing by round: 9,4,1)").action is Action.REWORK


def test_counts_stop_at_the_semicolon() -> None:
    """``2;`` is not a digit, and dropping the last count can flip the decision.

    The last count is the one the convergence test compares, so a trailing file
    reference must not truncate the list to the wrong answer.
    """
    detail = "(stalled after 3 round(s); failing by round: 9 5 4 2; see /g/rounds.tsv)"
    assert round_counts(detail) == [9, 5, 4, 2]
    assert decide_late(detail).action is Action.REWORK


def test_rework_budget_is_per_lane_and_allows_three() -> None:
    converging = "(stalled after 3 round(s); failing by round: 5 4 2)"
    for count in range(MAX_REWORK_PER_LANE):
        decision = decide_late(converging, counters=RouteCounters(rework_at_lane=count))
        assert decision.action is Action.REWORK
    assert (
        decide_late(converging, counters=RouteCounters(rework_at_lane=MAX_REWORK_PER_LANE)).action
        is Action.PARK
    )


# ---------------------------------------------------------------------------
# Rule 6: no-push / untestable -> rework once per head, then park
# ---------------------------------------------------------------------------

NO_PUSH_REASONS = [
    "(no-push after 1 round(s); failing by round: 3 3; see /g/rounds.tsv)",
    "(untestable-unresolved after 2 round(s); failing by round: 4 4; see /g/rounds.tsv)",
]


@pytest.mark.parametrize("reason", NO_PUSH_REASONS)
def test_no_push_reasons_rework(reason: str) -> None:
    decision = decide_late(reason)
    assert decision.verdict in (Verdict.NO_PUSH, Verdict.UNTESTABLE)
    assert decision.action is Action.REWORK


def test_no_push_counts_do_not_buy_convergence() -> None:
    """No-push gets exactly one rework regardless of the counts it carries.

    The counts belong to the stalled/cap rule; borrowing them here would let a
    lane that pushed nothing three times look like a converging one.
    """
    decision = decide_late("(no-push after 3 round(s); failing by round: 9 4 1)")
    assert decision.action is Action.REWORK


def test_no_push_twice_parks() -> None:
    decision = decide_late(
        "(no-push after 1 round(s); failing by round: 3 3)",
        counters=RouteCounters(rework_at_head=1),
    )
    assert decision.action is Action.PARK
    assert decision.exhausted


def test_no_push_budget_is_separate_from_the_lane_rework_budget() -> None:
    """Spending the lane's converging-lane budget must not park a no-push rework."""
    decision = decide_late(
        "(no-push after 1 round(s); failing by round: 3 3)",
        counters=RouteCounters(rework_at_lane=MAX_REWORK_PER_LANE),
    )
    assert decision.action is Action.REWORK


# ---------------------------------------------------------------------------
# Everything else parks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lines", "why"),
    [
        ((), "no verdicts at all"),
        (("09:00:00 start @0dc2391ab (PR #3544)",), "only progress chatter"),
        (("10:00:00 GATE-SKIPPED PR #3544 @0dc2391ab (gate disabled)",), "not a gate verdict"),
        (
            ("10:00:00 NEEDS-ESCALATION 3 test(s) fail on the rebased head",),
            "an unrecognised reason",
        ),
    ],
)
def test_anything_else_parks(lines: tuple[str, ...], why: str) -> None:
    assert decide(lines, head=HEAD).action is Action.PARK, why


def test_park_never_reports_a_spent_budget() -> None:
    """Park means "an agent will not help", which is different from "out of budget"."""
    decision = decide(("10:00:00 NEEDS-ESCALATION 3 test(s) fail on the rebased head",))
    assert not decision.exhausted


# ---------------------------------------------------------------------------
# The parsing underneath the table
# ---------------------------------------------------------------------------


def test_only_the_verdict_field_is_read_not_the_agent_s_tail() -> None:
    """An escalation line ends with the agent's own message, which is model text.

    An unanchored match lets a model quote ``PREMERGE-APPROVED`` into its summary
    and have the line classified as an approval.
    """
    spoofed = line("fail-closed: agent(s) died; I was about to write PREMERGE-APPROVED deadbeef")
    assert classify(spoofed)[0] is Verdict.INFRA
    assert decide((spoofed,), head=HEAD).action is Action.REGATE


def test_a_quoted_approval_token_is_not_an_approval() -> None:
    """A model quoting the marker into its own summary must not promote the line.

    The token has to be the verdict *field*, so the whole line is read as the
    reason and no marker in it can be mistaken for a verdict.
    """
    spoofed = line("(no-push after 1 round(s); I did not write PREMERGE-APPROVED deadbeef)")
    assert classify(spoofed)[0] is Verdict.NO_PUSH
    assert decide((spoofed,), head=HEAD).action is Action.REWORK


def test_progress_lines_are_not_terminal() -> None:
    assert not is_terminal("09:00:00 start @0dc2391ab (PR #3544)")
    assert not is_terminal("09:20:00 round 1 @0dc2391ab: failing 4 -> 2")
    assert is_terminal("10:00:00 NEEDS-ESCALATION whatever")
    assert is_terminal("10:00:00 PREMERGE-APPROVED 0dc2391ab")


def test_last_verdict_line_skips_chatter_and_finds_the_verdict() -> None:
    lines = (
        "09:00:00 start @0dc2391ab (PR #3544)",
        "10:00:00 NEEDS-ESCALATION fail-closed: agent(s) died",
        "10:00:01 reconcile: re-gate (infra) #2 @0dc2391ab",
    )
    assert "fail-closed" in last_verdict_line(lines)
    assert last_verdict_line(("09:00:00 start @0dc2391ab",)) == ""


def test_approved_sha_reads_the_sha_field() -> None:
    assert approved_sha("10:00:00 PREMERGE-APPROVED 0dc2391ab") == "0dc2391ab"
    assert approved_sha("10:00:00 NEEDS-ESCALATION nope") == ""


@pytest.mark.parametrize(
    ("counts", "converging"),
    [
        ([5, 4, 2], True),
        ([9, 4, 1], True),
        ([2, 3, 3], False),
        ([4, 4], False),
        ([3, 1, 5], False),
        ([7], False),
        ([], False),
    ],
)
def test_is_converging(counts: list[int], converging: bool) -> None:
    assert is_converging(counts) is converging


def test_decide_is_pure_and_returns_the_same_answer_twice() -> None:
    """The whole point of the port: the table is a value, not a side effect."""
    lines = ("10:00:00 NEEDS-ESCALATION (stalled after 3 round(s); failing by round: 5 4 2)",)
    first = decide(lines, head=HEAD, lane="x")
    second = decide(lines, head=HEAD, lane="x")
    assert first == second


def test_every_action_is_reachable() -> None:
    """A row that no case reaches is a row nothing tests, and a rule nothing uses."""
    reached = {
        decide_one("fail-closed: agent(s) died").action,
        decide_one("merged-tree regression check failed").action,
        decide_one("gate tests could not run at head (SUMMARY x: rc=2)").action,
        decide_one("(stalled after 3 round(s); failing by round: 5 4 2)").action,
        decide_one("nothing recognisable").action,
        decide((line("0dc2391ab", token="PREMERGE-APPROVED"),), head=HEAD).action,
    }
    assert reached == set(Action)
