"""Synthesizer phase module.

Collapses a list of ResearchNotes into a single ImplementationBrief that the
Implementer will use as its primary context.

The LLM is prompted with the TaskSpec and all research findings serialized as
JSON, and must return a valid ImplementationBrief JSON object.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from agent_fleet.contracts.implementation_brief import (
    ImplementationBrief,
    validate_implementation_brief,
)

if TYPE_CHECKING:
    from agent_fleet.contracts.research_note import ResearchNote
    from agent_fleet.contracts.task_spec import TaskSpec
    from agent_fleet.hooks import LLMBackend, LLMSession


def _extract_json(text: str) -> dict[str, Any]:
    """Extract the first JSON object from *text*.

    Scans for the first ``{`` and returns the balanced object.  Raises
    ``ValueError`` if no valid JSON object is found.
    """
    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in LLM output")

    # Walk forward tracking brace depth to find the matching closing brace.
    depth = 0
    in_string = False
    escape_next = False
    for i, ch in enumerate(text[start:], start=start):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"JSON parse error: {exc}") from exc

    # Fall back to a regex that finds ```json ... ``` fenced blocks.
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON parse error in fenced block: {exc}") from exc

    raise ValueError("No balanced JSON object found in LLM output")


# Paths that signal this PR needs a rollback plan.
_ROLLBACK_SIGNALS = (
    "migrations/",
    "schema/",
    ".sql",
    ".toml",
    ".yaml",
    ".yml",
    ".env",
    "fleet_config.",
    ".github/workflows/",
    "ops/",
    "scripts/deploy",
)


def _needs_rollback(files: list[str]) -> bool:
    """Return True if any file path matches a rollback-signal pattern."""
    return any(sig in path for path in files for sig in _ROLLBACK_SIGNALS)


_SYSTEM_PROMPT = """\
You are the Synthesizer in a multi-agent engineering fleet.

Your job: read the TaskSpec and the list of ResearchNotes below, then produce a
single ImplementationBrief JSON object that the Implementer agent will follow
exactly.

## TaskSpec (JSON)

{task_spec_json}

## ResearchNotes (JSON array)

{research_notes_json}

## Required output

Return ONLY a JSON object matching this schema (no markdown, no commentary):

{{
  "issue_number": <integer matching task_spec.issue_number>,
  "summary": "<concise implementation summary>",
  "files_to_create": ["<path>", ...],
  "files_to_modify": ["<path>", ...],
  "test_strategy": "<test approach>",
  "acceptance_criteria": ["<criterion>", ...],
  "references": [
    {{"research_id": "<id>", "key_finding": "<one-sentence finding>"}},
    ...
  ],
  "rollback_plan": "<numbered steps a human can execute under incident pressure, or null>"
}}

Rules:
- issue_number MUST equal the task_spec issue_number.
- acceptance_criteria MUST include every criterion from the TaskSpec.
- references MUST cite every research note by its research_id.
- Do not add fields beyond those listed.
- Do not invent file paths not supported by the research notes.
- rollback_plan: populate with a numbered list of concrete steps ONLY when the
  PR touches schemas/migrations (*.sql, migrations/, schema/), config files
  (*.toml, *.yaml, *.yml, *.env*, fleet_config.*), CI/CD workflows
  (.github/workflows/), or cross-system infrastructure (ops/, scripts/deploy*).
  For all other PRs, set rollback_plan to null.
  The numbered steps must be concrete and executable by an on-call engineer
  under incident pressure (e.g. "1. Revert the PR via 'git revert <sha>'").
{rollback_instruction}"""


def synthesize(
    task_spec: TaskSpec,
    research_notes: list[ResearchNote],
    *,
    backend: LLMBackend,
    max_tokens: int = 4096,
    timeout_s: int = 720,
    memory_limit: str = "2G",
    extra_context: str | None = None,
    session: LLMSession | None = None,
) -> ImplementationBrief:
    """Run the Synthesizer phase.

    Collapses *research_notes* into a single ImplementationBrief that the
    Implementer will use as its primary context. The LLM is prompted with
    the TaskSpec (acceptance_criteria, scope, risk_tier) and all research
    findings serialized as JSON, and must return an ImplementationBrief JSON.

    Returns a validated ImplementationBrief dataclass.
    Raises ValueError on JSON parse failure or schema validation error.
    """
    task_spec_json = json.dumps(task_spec.to_dict(), indent=2)
    research_notes_json = json.dumps([note.to_dict() for note in research_notes], indent=2)

    # Collect all paths from the task_spec scope and research notes to detect
    # whether this PR touches rollback-signal paths.
    all_paths: list[str] = list(task_spec.scope.allowed_paths)
    for note in research_notes:
        all_paths.extend(note.scope_paths)
        all_paths.extend(note.referenced_files)
    if _needs_rollback(all_paths):
        rollback_instruction = (
            "IMPORTANT: This PR touches rollback-sensitive paths. You MUST "
            "populate rollback_plan with a numbered list of concrete steps."
        )
    else:
        rollback_instruction = ""

    prompt = _SYSTEM_PROMPT.format(
        task_spec_json=task_spec_json,
        research_notes_json=research_notes_json,
        rollback_instruction=rollback_instruction,
    )

    if extra_context:
        prompt = f"{extra_context}\n\n{prompt}"

    data, raw = _run_with_json_retry(
        backend,
        prompt,
        max_tokens=max_tokens,
        timeout_s=timeout_s,
        memory_limit=memory_limit,
        session=session,
    )

    try:
        validate_implementation_brief(data)
    except Exception as exc:
        raise ValueError(
            f"Synthesizer: LLM output failed schema validation: {exc}\n"
            f"--- raw output (first 500 chars) ---\n{raw[:500]}"
        ) from exc

    return ImplementationBrief.from_dict(data)


def _run_with_json_retry(
    backend: LLMBackend,
    prompt: str,
    *,
    max_tokens: int,
    timeout_s: int,
    memory_limit: str,
    session: LLMSession | None = None,
) -> tuple[dict[str, Any], str]:
    """Invoke *backend* and parse JSON, retrying once if no JSON is found.

    The first retry re-prompts the LLM with an explicit "your previous output
    contained no JSON — return ONLY the JSON object" suffix. Without this,
    Composer occasionally returns conversational prose during verify_failed
    retries and the whole fleet run dies silently.
    """
    last_error: Exception | None = None
    raw = ""
    for attempt in range(2):
        current_prompt = prompt
        if attempt == 1:
            current_prompt = (
                f"{prompt}\n\n"
                "IMPORTANT: Your previous response contained no parseable JSON "
                "object. Return ONLY the JSON object — no prose, no markdown "
                "fences, no commentary. The response MUST start with '{' and "
                "end with '}'."
            )
        if session is not None:
            result = session.send(
                current_prompt,
                max_tokens=max_tokens,
                timeout_s=timeout_s,
                allowed_tools=[],
            )
        else:
            result = backend.run(
                current_prompt,
                max_tokens=max_tokens,
                timeout_s=timeout_s,
                memory_limit=memory_limit,
                allowed_tools=[],
            )
        raw = result.stdout
        try:
            return _extract_json(raw), raw
        except ValueError as exc:
            last_error = exc
            continue
    raise ValueError(
        f"Synthesizer: could not parse LLM output as JSON after 2 attempts: {last_error}\n"
        f"--- last raw output (first 500 chars) ---\n{raw[:500]}"
    )
