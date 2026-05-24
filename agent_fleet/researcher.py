"""Researcher phase module.

Responsible for running LLM-backed research questions and returning
validated ResearchNote dataclasses. Each question is independent;
research_all() runs them concurrently using a ThreadPoolExecutor.
"""

from __future__ import annotations

import contextlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from agent_fleet.contracts.mcp_requirement import browser_prompt_block
from agent_fleet.contracts.research_note import Confidence, ResearchNote, validate_research_note

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agent_fleet.hooks import LLMBackend, LLMSession

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
    session: LLMSession | None = None,
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
    browser_requirement = ""
    if needs_browser:
        browser_requirement = browser_prompt_block()

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
{browser_requirement}

Return ONLY the JSON object — no prose before or after it.
"""

    if session is not None:
        result = session.send(
            prompt,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
            allowed_tools=allowed_tools,
            expect_mcp_tools=needs_browser,
        )
    else:
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
    session: LLMSession | None = None,
) -> tuple[ResearchNote, float]:
    """Run one research item and return (note, duration_s)."""
    allowed_tools: list[str] = ["read_file"]
    if needs_browser:
        allowed_tools = ["read_file", "browse"]

    scope_list = "\n".join(f"  - {p}" for p in scope_paths)
    browser_hint = " (browser tools are available)" if needs_browser else ""
    browser_requirement = ""
    if needs_browser:
        browser_requirement = browser_prompt_block()

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
{browser_requirement}

Return ONLY the JSON object — no prose before or after it.
"""

    last_error: Exception | None = None
    raw_text = ""
    duration_total = 0.0
    raw: dict[str, Any] | None = None
    for attempt in range(3):
        current_prompt = prompt
        if attempt >= 1:
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
                allowed_tools=allowed_tools,
                expect_mcp_tools=needs_browser,
            )
        else:
            result = backend.run(
                current_prompt,
                max_tokens=max_tokens,
                timeout_s=timeout_s,
                memory_limit=memory_limit,
                allowed_tools=allowed_tools,
                cwd=cwd,
            )
        raw_text = result.stdout
        duration_total += result.duration_s
        # Defensive: backend-level failure should not even attempt JSON parse
        if result.exit_code != 0:
            last_error = ValueError(
                f"Backend error (exit {result.exit_code}): {result.stderr or 'unknown error'}"
            )
            continue
        try:
            raw = _extract_json(raw_text)
            break
        except ValueError as exc:
            last_error = exc
            continue
    if raw is None:
        raise ValueError(
            f"Researcher: could not parse LLM output as JSON after 3 attempts: "
            f"{last_error}\n--- last raw output (first 500 chars) ---\n{raw_text[:500]}"
        )
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
    return note, duration_total


def research_all(
    research_plan: list[dict[str, Any]],
    *,
    backend: LLMBackend,
    memory_limit: str = "2G",
    max_workers: int = 4,
    cwd: Path | None = None,
    session: LLMSession | None = None,
    browser_session_factory: Callable[[], LLMSession | None] | None = None,
) -> list[ResearchNote]:
    """Run all research items in *research_plan* and return notes in plan order.

    Code-only items run concurrently. ``needs_browser`` items run sequentially,
    each with a dedicated MCP session from *browser_session_factory*.
    """
    if not research_plan:
        return []

    browser_items = [item for item in research_plan if item.get("needs_browser")]
    code_items = [item for item in research_plan if not item.get("needs_browser")]
    notes_by_id: dict[str, ResearchNote] = {}

    def _run_item(item: dict[str, Any], item_session: LLMSession | None) -> ResearchNote:
        note, _ = _research_with_duration(
            item["id"],
            item["question"],
            item["scope_paths"],
            backend=backend,
            needs_browser=item.get("needs_browser", False),
            memory_limit=memory_limit,
            cwd=cwd,
            session=item_session,
        )
        return note

    if code_items:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(code_items))) as executor:
            futures = {executor.submit(_run_item, item, session): item for item in code_items}
            for future in futures:
                item = futures[future]
                notes_by_id[item["id"]] = future.result()

    for item in browser_items:
        browser_session: LLMSession | None = None
        if browser_session_factory is not None:
            browser_session = browser_session_factory()
        try:
            notes_by_id[item["id"]] = _run_item(item, browser_session)
        finally:
            if browser_session is not None:
                with contextlib.suppress(Exception):
                    browser_session.dispose()

    return [notes_by_id[item["id"]] for item in research_plan]
