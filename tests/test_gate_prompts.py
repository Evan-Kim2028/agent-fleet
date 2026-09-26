"""Every gate prompt must forbid commands that never exit.

A gate agent that runs ``tail -f``, ``watch``, or a dev server in the
foreground never returns, so its slot is held and its answer never arrives.
The gate already fails closed when that happens (a dead agent escalates),
but the run is wasted: the stage burns its whole budget and produces no
verdict.

The fix is to say so up front, in the same preamble that already carries
the process-safety rule, and to apply it to all five roles — including the
fixer, which is the stage most likely to start a long-running process to
see whether its change works.
"""

from __future__ import annotations

from agent_fleet.contracts.gate import Finding
from agent_fleet.gate import prompts

#: A phrase every prompt must carry, whichever role it was built for.
RULE_MARKER = "NO BLOCKING COMMANDS"


def _all_prompts() -> dict[str, str]:
    finding = Finding(id="c-1", file="a.py", line=2, claim="wrong", repro="x -> y")
    return {
        "find": prompts.find_prompt(
            lens="correctness",
            focus="logic errors",
            worktree="/tmp/wt",
            base_branch="origin/main",
            head_sha="abc123def",
            pr_number=7,
            task_text="the task",
        ),
        "verify": prompts.verify_prompt(
            finding=finding,
            worktree="/tmp/wt",
            base_branch="origin/main",
            head_sha="abc123def",
            pr_number=7,
            test_dir_hint="tests",
            pytest_cmd_hint="(cd /tmp/wt && pytest -q tests/)",
            test_file_name="test_gate_fb_lane_c_1.py",
        ),
        "judge": prompts.judge_prompt(
            worktree="/tmp/wt",
            base_branch="origin/main",
            head_sha="abc123def",
            pr_number=7,
            confirmed="(none)",
            untestable="(none)",
            task_text="the task",
        ),
        "recheck": prompts.recheck_prompt(
            worktree="/tmp/wt",
            head_sha="abc123def",
            pr_number=7,
            start_sha="000000000",
            untestable="(none)",
        ),
        "fix": prompts.fix_prompt(
            pr_number=7,
            worktree="/tmp/wt",
            head_sha="abc123def",
            push_branch="fb/lane",
            round_number=1,
            failing="tests/test_gate_fb_lane_c_1.py::test_x",
            confirmed="(none)",
            untestable="(none)",
            all_tests="tests/test_pr.py",
            pytest_cmd_hint="(cd /tmp/wt && pytest -q tests/)",
            task_text="the task",
        ),
    }


# ---------------------------------------------------------------------------
# The rule reaches every role
# ---------------------------------------------------------------------------


def test_every_role_prompt_forbids_blocking_commands() -> None:
    for role, prompt in _all_prompts().items():
        assert RULE_MARKER in prompt, f"the {role} prompt lost the rule"


def test_the_rule_names_the_commands_that_never_exit() -> None:
    """Naming the offenders is what makes the rule actionable, not just present."""
    rule = prompts.NO_BLOCKING_COMMANDS
    for offender in ("tail -f", "watch", "pager", "foreground"):
        assert offender in rule, offender


def test_the_rule_requires_a_bounded_poll_instead() -> None:
    assert "bounded" in prompts.NO_BLOCKING_COMMANDS
    assert "timeout" in prompts.NO_BLOCKING_COMMANDS


# ---------------------------------------------------------------------------
# The rule composes with the process-safety rule rather than replacing it
# ---------------------------------------------------------------------------


def test_every_role_prompt_still_forbids_pattern_kills() -> None:
    """Regression: a lens once ran `pkill -9 -f pytest` and killed every other
    agent whose argv held a prompt. Both rules must be present."""
    for role, prompt in _all_prompts().items():
        assert "NEVER kill processes by name or pattern" in prompt, role


def test_the_rules_are_adjacent_at_the_head_of_every_prompt() -> None:
    """Both rules are preamble: they have to be read before the task, not
    interleaved with it."""
    head = prompts.AGENT_RULES
    assert head.index("NO BLOCKING COMMANDS") < head.index("PROCESS SAFETY")
    for role, prompt in _all_prompts().items():
        assert prompt.startswith(head), role


def test_the_shared_prefix_is_used_by_all_five_prompt_builders() -> None:
    """If a sixth role is added later it must inherit the rules by construction."""
    from pathlib import Path

    src = Path(prompts.__file__).read_text(encoding="utf-8")
    assert src.count("return AGENT_RULES + (") == 5


def test_the_required_format_still_comes_after_the_rules() -> None:
    """The preamble must not displace the answer contract.

    A structured role's format instruction and example have to appear after the
    rules, since that is the part the model copies. (``fix`` is free-form and
    legitimately has no JSON block; ``find`` ends with a note on ``testable``.)
    """
    for role in ("verify", "judge", "recheck"):
        prompt = _all_prompts()[role]
        assert prompt.index(prompts.AGENT_RULES) < prompt.index("exactly one fenced json block")
        assert prompt.rstrip().endswith("```")
