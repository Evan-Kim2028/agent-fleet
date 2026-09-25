"""Standing fences must ride along with every implementer prompt.

A fence that can go missing when a scratchpad is cleaned is not a fence, so
these live in code. The test asserts each fence the owner actually declared is
present, because a fence dropped from the list is invisible until an agent walks
into it.
"""

from __future__ import annotations

from agent_fleet.fleet_ops.fences import DEFAULT_FENCES, FENCES_HEADER, render_fences


def test_every_declared_fence_ships() -> None:
    rendered = render_fences()
    for fence in DEFAULT_FENCES:
        assert fence in rendered, fence


def test_the_fences_named_in_the_owners_standing_set_are_present() -> None:
    """Requirement 7, checked fence by fence against the standing set."""
    rendered = render_fences().lower()
    assert "git stash" in rendered
    assert "--no-verify" in rendered
    assert "gold.sales" in rendered
    assert "2026-08-01" in rendered
    assert "in-place stamp-column update" in rendered
    assert "08:30" in rendered and "12:00 utc" in rendered
    assert "memorymax=6g" in rendered
    assert "targeted test files" in rendered
    assert "never auto-allowlisted" in rendered
    assert "io pressure" in rendered


def test_the_header_is_the_documented_one() -> None:
    assert FENCES_HEADER in render_fences()


def test_each_fence_is_a_bullet() -> None:
    lines = render_fences().splitlines()
    assert lines[0] == FENCES_HEADER
    assert all(line.startswith("- ") for line in lines[1:])


def test_repo_fences_are_appended_not_substituted() -> None:
    """A repo may add a rule; it can never shorten the house rules."""
    rendered = render_fences(("Repo rule: never edit the vendor tree.",))
    assert "Repo rule: never edit the vendor tree." in rendered
    for fence in DEFAULT_FENCES:
        assert fence in rendered


def test_extra_fences_come_last() -> None:
    rendered = render_fences(("Zebra rule.",))
    assert rendered.index("Zebra rule.") > rendered.index(DEFAULT_FENCES[-1])


def test_blank_extras_are_ignored() -> None:
    """A blank config entry must not become an empty bullet in the prompt."""
    rendered = render_fences(("", "   "))
    assert rendered.count("\n- ") == len(DEFAULT_FENCES)
