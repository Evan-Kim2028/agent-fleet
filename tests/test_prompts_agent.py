"""Tests for shared agent prompt assembly."""

from __future__ import annotations

from agent_fleet.prompts.agent import build_agent_prompt


def test_build_agent_prompt_minimal() -> None:
    result = build_agent_prompt(
        persona_body="You are a coder.",
        task_heading="Task",
        task_body="Fix the bug.",
    )

    assert result.persona_section == "# Persona\nYou are a coder."
    assert result.task_section == "\n# Task\nFix the bug."
    assert result.full == "# Persona\nYou are a coder.\n# Task\nFix the bug."


def test_build_agent_prompt_includes_context_and_closing() -> None:
    closing = (
        "Execute this task in the workspace. Return a concise summary of what you "
        "did, files changed, and any follow-up needed."
    )
    result = build_agent_prompt(
        persona_body="You are a coder.",
        task_heading="Task",
        task_body="Fix the bug.",
        context="See auth.py",
        closing_instruction=closing,
    )

    assert "# Context\nSee auth.py" in result.full
    assert result.full.endswith(closing)
    assert "# Task\nFix the bug." in result.task_section


def test_build_agent_prompt_includes_persona_extras() -> None:
    result = build_agent_prompt(
        persona_body="You are a coder.",
        task_heading="Task",
        task_body="Fix the bug.",
        extra_instructions="Prefer small diffs.",
        allowed_paths=("src/**", "tests/**"),
    )

    assert "# Additional Instructions\nPrefer small diffs." in result.persona_section
    assert "# Scope: only modify paths matching: src/**, tests/**" in result.persona_section


def test_build_agent_prompt_supports_extra_sections() -> None:
    result = build_agent_prompt(
        persona_body="You are a reviewer.",
        task_heading="Original Task",
        task_body="Add logging.",
        extra_sections=[
            ("Implementation Summary", "Added structured logs."),
            ("Review", "Check for PII in logs."),
        ],
    )

    assert "# Implementation Summary\nAdded structured logs." in result.full
    assert "# Review\nCheck for PII in logs." in result.full


def test_build_agent_prompt_skips_blank_extra_sections() -> None:
    result = build_agent_prompt(
        persona_body="You are a reviewer.",
        task_heading="Task",
        task_body="Review changes.",
        extra_sections=[("Review", "   "), ("Notes", "Keep it concise.")],
    )

    assert "# Review" not in result.full
    assert "# Notes\nKeep it concise." in result.full
