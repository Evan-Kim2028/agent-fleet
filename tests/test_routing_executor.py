"""The two agent-backed routing actions: what the agent is told, and what is left behind.

Nothing here runs an agent, a worktree, or git. What is worth testing is the
part that is decided *before* the agent starts — the two rules the policy
depends on and cannot enforce after the fact: which side of a colliding
``test_gate_*.py`` survives, and that a repair is told never to weaken an
assertion. A prompt that loses either rule produces a PR that passes the gate by
looking right rather than being right, which no test after the push would catch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_fleet.routing.executor import Mode, build_prompt, resolve_lane


def prompt_for(mode: Mode) -> str:
    return build_prompt(
        mode,
        pr_number=3544,
        head_ref="fb/gate-routing",
        base_ref="main",
        worktree=Path("/wt/agent-fleet-wt-fb-gate-routing"),
        lane="gate-routing",
    )


# ---------------------------------------------------------------------------
# Lane folding — the counter key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("head_ref", "lane"),
    [
        ("fb/gate-routing", "gate-routing"),
        ("fb/a/b/c", "a/b/c"),
        ("main", "main"),
        ("fb/", "fb/"),
        ("", "lane"),
    ],
)
def test_resolve_lane_folds_the_fb_prefix(head_ref: str, lane: str) -> None:
    assert resolve_lane(head_ref) == lane


# ---------------------------------------------------------------------------
# The gate-test collision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", list(Mode))
def test_both_sides_of_a_gate_test_collision_survive(mode: Mode) -> None:
    text = prompt_for(mode)
    assert "KEEPING BASE'S VERSION" in text
    assert "RENAMING" in text
    assert "Never delete either side's test" in text


@pytest.mark.parametrize("mode", list(Mode))
def test_the_rename_uses_the_lane_token_the_gate_would_produce(mode: Mode) -> None:
    """The agent is told the concrete name, not a shape to infer.

    ``test_gate_<lane>_<suffix>.py`` is what :func:`gate_test_name` produces for
    this lane, and it is the name the gate will look for when it archives the
    evidence — a rename to anything else loses the test's provenance.
    """
    from agent_fleet.gate.prompts import lane_slug_token

    text = prompt_for(mode)
    assert f"test_gate_{lane_slug_token('gate-routing')}_<suffix>.py" in text


def test_both_modes_merge_the_base_first() -> None:
    """A repair that skipped the merge would fix the tests against a head that
    base is about to invalidate, and the merged-tree check would fail next run."""
    for mode in Mode:
        text = prompt_for(mode)
        assert "git fetch origin main" in text
        assert "git merge origin/main" in text
        assert "` into fb/gate-routing" in text


# ---------------------------------------------------------------------------
# Repair must not weaken the tests
# ---------------------------------------------------------------------------


def test_repair_fences_assertion_weakening() -> None:
    text = prompt_for(Mode.REPAIR)
    assert "never weaken an assertion" in text.lower()
    assert "xfail" in text
    assert "collection error" in text
    assert "fix the product code, never the assertion" in text


def test_rebase_does_not_ask_for_a_test_repair() -> None:
    """A rebase that also rewrote tests would fix something the gate never asked about."""
    text = prompt_for(Mode.REBASE)
    assert "never weaken an assertion" not in text.lower()
    assert "BOTH sides survive" in text


# ---------------------------------------------------------------------------
# Commits
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", list(Mode))
def test_no_hook_is_ever_disabled(mode: Mode) -> None:
    text = prompt_for(mode)
    assert "NEVER `git commit --no-verify`" in text
    # The one permitted escape is a *named* hook, and only for baseline debt.
    assert "SKIP=<hook-id>" in text
    assert "never disable hooks" in text.lower()


@pytest.mark.parametrize("mode", list(Mode))
def test_the_agent_pushes_to_the_prs_own_head_ref(mode: Mode) -> None:
    """Pushing anywhere else opens a second PR and leaves this one stale."""
    assert "git push origin HEAD:fb/gate-routing" in prompt_for(mode)


@pytest.mark.parametrize("mode", list(Mode))
def test_process_safety_rules_are_prepended(mode: Mode) -> None:
    """This machine runs many agents; the shared preamble is a precondition."""
    text = prompt_for(mode)
    assert text.index("PROCESS SAFETY") < text.index("You are working in")
    assert "NO BLOCKING COMMANDS" in text


# ---------------------------------------------------------------------------
# What a success leaves behind
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (Mode.REBASE, "fail-closed: rebase agent pushed; re-gate"),
        (Mode.REPAIR, "fail-closed: repair agent pushed; re-gate"),
    ],
)
def test_the_escalation_line_names_the_mode_and_demands_a_re_gate(
    mode: Mode, expected: str
) -> None:
    """Fail-closed, so the policy classifies it as infra and re-gates.

    A rebase or repair can therefore never merge anything: it can only put the
    PR back in front of a gate that has to approve the new head from scratch.
    """
    from agent_fleet.routing.executor import _status_line

    line = _status_line(mode)
    assert expected in line
    assert "NEEDS-ESCALATION" in line


# ---------------------------------------------------------------------------
# Which engine runs
# ---------------------------------------------------------------------------


def test_the_routing_agent_runs_on_the_gates_own_backend() -> None:
    """It fixes what the gate found, so it runs where the gate's fixes run.

    Reading the backend from anywhere else would let a rebase or repair use a
    model the gate's policy has not allowlisted for fixing its own findings.
    """
    from agent_fleet.gate.config import GateConfig, load_gate_config
    from agent_fleet.gate.pipeline import _load_raw_config
    from agent_fleet.routing.executor import _default_backend

    expected = (load_gate_config(_load_raw_config(None)) or GateConfig()).backend
    assert expected
    # Resolution must not raise on a repo with no gate config at all.
    assert _default_backend() is not None
