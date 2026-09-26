"""Routing attempt counters: the budgets, keyed and read the reconciler's way.

The counter files are shared with ``pr_triage.py`` and the bash reconciler, so
the on-disk format is a contract, not an implementation detail: one
``<lane> <sha9>`` record per line, appended, never rewritten. A counter that
reads as zero when it cannot be read would hand every lane a fresh budget, which
is the one failure mode these tests exist to catch.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 - pytest resolves the annotation at runtime

import pytest

from agent_fleet.routing.counters import (
    REBASE_LOG,
    REPAIR_LOG,
    RoutingError,
    read_counters,
    record_attempt,
)

HEAD = "0dc2391ab3c4"
OTHER_HEAD = "fffffffffff"


def test_a_fresh_lane_has_spent_nothing(tmp_path: Path) -> None:
    counters = read_counters("gate-routing", HEAD, home=tmp_path)
    assert counters.to_dict() == {
        "regate_at_head": 0,
        "rebase_at_head": 0,
        "repair_at_head": 0,
        "rework_at_head": 0,
        "rework_at_lane": 0,
    }


@pytest.mark.parametrize("kind", ["regate", "rework", "rebase", "repair"])
def test_attempts_are_recorded_per_head(tmp_path: Path, kind: str) -> None:
    record_attempt(kind, "gate-routing", HEAD, home=tmp_path)
    record_attempt(kind, "gate-routing", HEAD, home=tmp_path)
    counters = read_counters("gate-routing", HEAD, home=tmp_path)
    assert getattr(counters, f"{kind}_at_head") == 2


def test_a_new_push_resets_the_budget(tmp_path: Path) -> None:
    """Three tries at *this* code is the rule; a new head is new code."""
    for _ in range(3):
        record_attempt("regate", "gate-routing", HEAD, home=tmp_path)
    assert read_counters("gate-routing", HEAD, home=tmp_path).regate_at_head == 3
    assert read_counters("gate-routing", OTHER_HEAD, home=tmp_path).regate_at_head == 0


def test_one_lane_does_not_spend_anothers_budget(tmp_path: Path) -> None:
    record_attempt("regate", "gate-routing", HEAD, home=tmp_path)
    assert read_counters("other-lane", HEAD, home=tmp_path).regate_at_head == 0


def test_a_short_sha_prefix_matches_the_full_record(tmp_path: Path) -> None:
    """The status file carries sha9; the counter file may carry more."""
    record_attempt("regate", "gate-routing", HEAD, home=tmp_path)
    assert read_counters("gate-routing", "0dc2391", home=tmp_path).regate_at_head == 1


def test_the_lane_rework_budget_spans_heads(tmp_path: Path) -> None:
    """The point of a per-lane cap: stop a lane spending the fleet one head at a time."""
    for _ in range(3):
        record_attempt("rework_lane", "gate-routing", home=tmp_path)
    assert read_counters("gate-routing", HEAD, home=tmp_path).rework_at_lane == 3
    assert read_counters("gate-routing", OTHER_HEAD, home=tmp_path).rework_at_lane == 3


def test_attempt_kinds_do_not_share_a_budget(tmp_path: Path) -> None:
    record_attempt("rebase", "gate-routing", HEAD, home=tmp_path)
    counters = read_counters("gate-routing", HEAD, home=tmp_path)
    assert counters.rebase_at_head == 1
    assert counters.repair_at_head == 0
    assert counters.regate_at_head == 0


def test_rebase_and_repair_use_separate_files(tmp_path: Path) -> None:
    record_attempt("rebase", "gate-routing", HEAD, home=tmp_path)
    record_attempt("repair", "gate-routing", HEAD, home=tmp_path)
    assert (tmp_path / REBASE_LOG).read_text().splitlines() == [f"gate-routing {HEAD}"]
    assert (tmp_path / REPAIR_LOG).exists()


def test_the_file_is_append_only(tmp_path: Path) -> None:
    """A consumer tailing the file mid-lane must still see the earlier records."""
    record_attempt("regate", "gate-routing", HEAD, home=tmp_path)
    record_attempt("regate", "gate-routing", HEAD, home=tmp_path)
    assert (tmp_path / "requeued_failclosed.txt").read_text().splitlines() == [
        f"gate-routing {HEAD}",
        f"gate-routing {HEAD}",
    ]


def test_junk_lines_are_ignored_not_counted(tmp_path: Path) -> None:
    log = tmp_path / "requeued_failclosed.txt"
    log.write_text("not a record\ngate-routing short\ngate-routing " + HEAD + "\n")
    assert read_counters("gate-routing", HEAD, home=tmp_path).regate_at_head == 1


def test_an_unknown_attempt_kind_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown attempt kind"):
        record_attempt("teleport", "gate-routing", HEAD, home=tmp_path)


def test_an_unreadable_counter_raises_rather_than_reading_as_zero(tmp_path: Path) -> None:
    """A budget that reads as zero on a read error is an unlimited budget."""
    (tmp_path / "requeued_failclosed.txt").mkdir()
    with pytest.raises(RoutingError, match="could not read routing counter"):
        read_counters("gate-routing", HEAD, home=tmp_path)


def test_an_unwritable_counter_is_fatal_to_the_action(tmp_path: Path) -> None:
    """Recording must succeed or the action must not run — a cap that cannot be
    charged would hand out the same budget again on the next pass."""
    (tmp_path / "reworked.txt").mkdir()
    with pytest.raises(RoutingError, match="could not record"):
        record_attempt("rework", "gate-routing", HEAD, home=tmp_path)


def test_creating_the_ops_home_is_done_for_you(tmp_path: Path) -> None:
    target = tmp_path / "not" / "yet"
    record_attempt("regate", "gate-routing", HEAD, home=target)
    assert read_counters("gate-routing", HEAD, home=target).regate_at_head == 1
