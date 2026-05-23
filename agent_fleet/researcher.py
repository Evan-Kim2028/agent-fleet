"""Researcher phase module.

Responsible for running LLM-backed research questions and returning
validated ResearchNote dataclasses. Each question is independent;
research_all() runs them concurrently using a ThreadPoolExecutor.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from agent_fleet.contracts.research_note import Confidence, ResearchNote, validate_research_note

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.hooks import LLMBackend

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict[str, Any]:
    """Extract the first JSON object from *text*.

    Strips markdown code fences if present, then finds the first ``{…}`` block.
    Raises ``ValueError`` on parse failure.

    Note: each phase module implements its own copy to keep file ownership
    disjoint and avoid a shared utility import that could cause merge conflicts
    in Wave 2.
    """
    # Strip optional markdown fences: ```json … ``` or ``` … ```
    stripped = re.sub(r"```(?:json)?\s*", "", text)
    match = _JSON_OBJECT_RE.search(stripped)
    if not match:
        raise ValueError(f"No JSON object found in LLM output: {text!r}")
    try:
        return json.loads(match.group())
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON parse error: {exc}") from exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def research(
    research_id: str,
    question: str,
    scope_paths: list[str],
    *,
    backend: LLMBackend,
    needs_browser: bool = False,
    max_tokens: int = 2048,
    timeout_s: int = 720,
    memory_limit: str = "2G",
    cwd: Path | None = None,
) -> ResearchNote:
    """Run one Researcher sub-phase for a single research question.

    Calls the LLM with a prompt asking it to investigate *question* within
    *scope_paths* and return a ResearchNote JSON object. Validates against
    the research_note schema.

    *needs_browser* is passed through to the prompt as a hint; the mock
    backend ignores it, the real Kimi backend may enable browser tools.

    Returns a validated ResearchNote dataclass.
    Raises ValueError on JSON parse failure or schema validation error.
    """
    allowed_tools: list[str] = ["read_file"]
    if needs_browser:
        allowed_tools = ["read_file", "browse"]

    scope_list = "\n".join(f"  - {p}" for p in scope_paths)
    browser_hint = " (browser tools are available)" if needs_browser else ""

    prompt = f"""\
You are a researcher agent{browser_hint}. Investigate the following question and
return your findings as a single JSON object matching this exact schema:

{{
  "research_id": "<string — use the id: {research_id}>",
  "question": "<the research question verbatim>",
  "findings": "<detailed findings as a string>",
  "scope_paths": ["<list of paths you were asked to scope to>"],
  "referenced_files": ["<list of files you actually read, if any>"],
  "confidence": "<one of: low, medium, high>"
}}

Research ID: {research_id}
Question: {question}
Scope paths:
{scope_list}

Return ONLY the JSON object — no prose before or after it.
"""

    result = backend.run(
        prompt,
        max_tokens=max_tokens,
        timeout_s=timeout_s,
        memory_limit=memory_limit,
        allowed_tools=allowed_tools,
        cwd=cwd,
    )

    raw = _extract_json(result.stdout)

    # Override research_id from the argument (authoritative source)
    raw["research_id"] = research_id

    try:
        validate_research_note(raw)
    except Exception as exc:
        raise ValueError(f"ResearchNote schema validation failed: {exc}") from exc

    return ResearchNote(
        research_id=raw["research_id"],
        question=raw["question"],
        findings=raw["findings"],
        scope_paths=list(raw["scope_paths"]),
        referenced_files=list(raw["referenced_files"]),
        confidence=Confidence(raw["confidence"]),
    )


def _research_with_duration(
    research_id: str,
    question: str,
    scope_paths: list[str],
    *,
    backend: LLMBackend,
    needs_browser: bool,
    memory_limit: str,
    max_tokens: int = 2048,
    timeout_s: int = 720,
    cwd: Path | None = None,
) -> tuple[ResearchNote, float]:
    """Run one research item and return (note, duration_s)."""
    allowed_tools: list[str] = ["read_file"]
    if needs_browser:
        allowed_tools = ["read_file", "browse"]

    scope_list = "\n".join(f"  - {p}" for p in scope_paths)
    browser_hint = " (browser tools are available)" if needs_browser else ""

    prompt = f"""\
You are a researcher agent{browser_hint}. Investigate the following question and
return your findings as a single JSON object matching this exact schema:

{{
  "research_id": "<string — use the id: {research_id}>",
  "question": "<the research question verbatim>",
  "findings": "<detailed findings as a string>",
  "scope_paths": ["<list of paths you were asked to scope to>"],
  "referenced_files": ["<list of files you actually read, if any>"],
  "confidence": "<one of: low, medium, high>"
}}

Research ID: {research_id}
Question: {question}
Scope paths:
{scope_list}

Return ONLY the JSON object — no prose before or after it.
"""

    result = backend.run(
        prompt,
        max_tokens=max_tokens,
        timeout_s=timeout_s,
        memory_limit=memory_limit,
        allowed_tools=allowed_tools,
        cwd=cwd,
    )

    raw = _extract_json(result.stdout)
    raw["research_id"] = research_id

    try:
        validate_research_note(raw)
    except Exception as exc:
        raise ValueError(f"ResearchNote schema validation failed: {exc}") from exc

    note = ResearchNote(
        research_id=raw["research_id"],
        question=raw["question"],
        findings=raw["findings"],
        scope_paths=list(raw["scope_paths"]),
        referenced_files=list(raw["referenced_files"]),
        confidence=Confidence(raw["confidence"]),
    )
    return note, result.duration_s


def research_all(
    research_plan: list[dict[str, Any]],
    *,
    backend: LLMBackend,
    memory_limit: str = "2G",
    max_workers: int = 4,
    cwd: Path | None = None,
) -> list[ResearchNote]:
    """Run all research items in *research_plan* concurrently, return all notes.

    Each plan item must have: id, question, scope_paths, needs_browser.
    Uses a ThreadPoolExecutor with *max_workers* to run research items in
    parallel, bounded by the light-subagent concurrency cap.
    """
    if not research_plan:
        return []

    def _capture_duration(item: dict[str, Any]) -> ResearchNote:
        note, _dur = _research_with_duration(
            item["id"],
            item["question"],
            item["scope_paths"],
            backend=backend,
            needs_browser=item.get("needs_browser", False),
            memory_limit=memory_limit,
            cwd=cwd,
        )
        return note

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # executor.map preserves plan order: results are yielded in the same order
        # as the input research_plan, while still running items concurrently.
        return list(executor.map(_capture_duration, research_plan))

