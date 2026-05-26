"""Tests for shared agent prompt assembly."""

from __future__ import annotations

from agent_fleet.prompts.agent import AgentPrompt, build_agent_prompt


def test_build_agent_prompt_minimal() -> None:
    result = build_agent_prompt(
        persona_body="You are a coder.",
        task_heading="Task",
        task_body="Fix the bug.",
    )

    assert isinstance(result, AgentPrompt)
    assert result.persona_section == "# Persona\nYou are a coder."
    assert "# Task\nFix the bug." in result.full
    assert result.full == result.persona_section + result.task_section
    assert "# Context" not in result.full


def test_build_agent_prompt_includes_context_and_extra_sections() -> None:
    result = build_agent_prompt(
        persona_body="Reviewer persona.",
        task_heading="Original Task",
        task_body="Ship the feature.",
        context="branch=main",
        extra_sections=[
            ("Additional Instructions", "Be thorough."),
            ("Scope: only modify paths matching: src/", ""),
        ],
    )

    assert "# Additional Instructions\nBe thorough." in result.full
    assert "# Scope: only modify paths matching: src/" in result.full
    assert "# Context\nbranch=main" in result.full
    assert result.full.index("# Additional Instructions") < result.full.index("# Original Task")
    assert result.full.index("# Original Task") < result.full.index("# Context")


def test_build_agent_prompt_emits_heading_for_empty_body_section() -> None:
    result = build_agent_prompt(
        persona_body="You are a coder.",
        task_heading="Task",
        task_body="Fix the bug.",
        extra_sections=[("Scope:", "")],
    )

    assert "# Scope:" in result.task_section


def test_build_agent_prompt_strips_whitespace() -> None:
    result = build_agent_prompt(
        persona_body="  persona  ",
        task_heading="Task",
        task_body="  goal  ",
        context="  ctx  ",
    )

    assert result.persona_section == "# Persona\npersona"
    assert "# Task\ngoal" in result.full
    assert "# Context\nctx" in result.full
