"""Two long branch names that share a 24-char prefix collide in the gate test name.

The whole point of putting the lane slug in a gate test file name is that two PRs
gating different branches never write the same path into the same test directory.
``slugify`` truncates to ``SLUG_MAX = 24`` characters, so the slug is not a
function of the branch name once a branch is longer than the cap: any two
branches agreeing on their first 24 folded characters produce one identical file
name, and the shared ``GateTestArchive`` then overwrites one lane's evidence with
the other's.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 - built into concrete paths at runtime

from agent_fleet.gate.pipeline import GateTestArchive
from agent_fleet.gate.prompts import SLUG_MAX, gate_test_name

#: Realistic branch names. Both fold to `fb_very_long_branch_name` + a tail of
#: 14 x/y characters, so the first 24 folded characters are identical and only
#: the truncated tail differs.
BRANCH_A = "fb/very-long-branch-name-xxxxxxxxxxxxxx"
BRANCH_B = "fb/very-long-branch-name-xxxxxxxxxxxxxy"

FINDING_ID = "c-1"


def test_a_long_branch_name_is_longer_than_the_slug_cap() -> None:
    """Guard the premise: the collision needs branches past SLUG_MAX."""
    assert len(BRANCH_A) > SLUG_MAX
    assert len(BRANCH_B) > SLUG_MAX
    assert BRANCH_A != BRANCH_B


def test_gate_test_name_distinguishes_two_long_branch_names() -> None:
    """The defect: the 24-char cap discards the only part that differs."""
    name_a = gate_test_name(BRANCH_A, FINDING_ID)
    name_b = gate_test_name(BRANCH_B, FINDING_ID)
    assert name_a != name_b, (
        f"distinct branches produced one gate test name {name_a!r}; "
        "every PR pair sharing a 24-char branch prefix collides"
    )


def test_archived_gate_tests_from_two_long_branch_names_both_survive(
    tmp_path: Path,
) -> None:
    """The consequence: the shared archive keys on the file name, so the second
    lane's store overwrites the first lane's confirmed-defect evidence."""
    archive = GateTestArchive(tmp_path / "gate")
    first = tmp_path / "wt-a"
    second = tmp_path / "wt-b"
    first.mkdir()
    second.mkdir()

    written_a = first / gate_test_name(BRANCH_A, FINDING_ID)
    written_b = second / gate_test_name(BRANCH_B, FINDING_ID)
    written_a.write_text("# lane A evidence\n")
    written_b.write_text("# lane B evidence\n")

    archive.store(written_a)
    archive.store(written_b)

    stored = sorted(p.name for p in archive.dir.iterdir())
    assert len(stored) == 2, f"one lane's evidence was overwritten; archived: {stored}"
