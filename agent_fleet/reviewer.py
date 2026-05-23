"""Fleet Reviewer phase module.

Reads a PR diff and changed-file list, calls the LLM backend to produce a
ReviewResult.  When the number of changed files exceeds *fanout_threshold* the
diff is reviewed in per-top-level-directory shards so that each LLM call stays
within a manageable context window.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from agent_fleet.contracts.review import ReviewResult, ReviewVerdict, validate_review

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.hooks import LLMBackend

# Files-changed threshold above which reviewer shards into multiple LLM calls.
DEFAULT_FANOUT_THRESHOLD = 20


def _extract_json(text: str) -> dict[str, Any]:
    """Extract the first JSON object from *text*.

    Scans for the opening ``{`` and uses a brace-depth counter to find the
    matching ``}``.  Raises ``ValueError`` if no valid JSON object is found.
    """
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object found in LLM output")

    depth = 0
    for i, ch in enumerate(text[start:], start):
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

    raise ValueError("unterminated JSON object in LLM output")


def _shard_by_directory(files: list[str]) -> dict[str, list[str]]:
    """Group *files* by their top-level directory component.

    Files without a directory component (e.g. ``'README.md'``) go in shard
    ``'_root'``.
    """
    shards: dict[str, list[str]] = {}
    for f in files:
        parts = f.split("/")
        key = parts[0] if len(parts) > 1 else "_root"
        shards.setdefault(key, []).append(f)
    return shards


def _build_prompt(
    pr_number: int,
    shard_files: list[str],
    pr_diff: str,
    shard_id: str | None,
) -> str:
    """Return the reviewer prompt for a single LLM call."""
    shard_note = (
        f"You are reviewing shard '{shard_id}' (files listed below)."
        if shard_id is not None
        else "You are reviewing the entire pull request."
    )
    files_block = "\n".join(f"  - {f}" for f in shard_files)
    return (
        f"You are a senior code reviewer for pull request #{pr_number}.\n"
        f"{shard_note}\n\n"
        f"Files in scope for this review:\n{files_block}\n\n"
        f"Full PR diff (for global context):\n{pr_diff}\n\n"
        "Return ONLY a JSON object with these fields:\n"
        "  pr_number   (integer) — the PR number above\n"
        "  verdict     (string)  — one of: approve | block | request_changes\n"
        "  summary     (string)  — concise review summary\n"
        "  issues      (array)   — each item: {severity, file, message}\n"
        "                          severity: low | medium | high\n"
        f"  shard_id    (string|null) — {json.dumps(shard_id)}\n"
        "No additional text outside the JSON object."
    )


def _call_backend(
    pr_number: int,
    shard_files: list[str],
    pr_diff: str,
    shard_id: str | None,
    *,
    backend: LLMBackend,
    max_tokens: int,
    timeout_s: int,
    memory_limit: str,
    cwd: Path | None = None,
) -> ReviewResult:
    """Issue one LLM call and parse the result into a ReviewResult."""
    prompt = _build_prompt(pr_number, shard_files, pr_diff, shard_id)
    result = backend.run(
        prompt,
        max_tokens=max_tokens,
        timeout_s=timeout_s,
        memory_limit=memory_limit,
        allowed_tools=[],
        cwd=cwd,
    )
    raw = _extract_json(result.stdout)
    # Enforce shard_id matches what we requested.
    raw["shard_id"] = shard_id
    raw["pr_number"] = pr_number
    validate_review(raw)
    return ReviewResult(
        pr_number=raw["pr_number"],
        verdict=ReviewVerdict(raw["verdict"]),
        summary=raw["summary"],
        issues=list(raw["issues"]),
        shard_id=raw["shard_id"],
    )


def review(
    pr_number: int,
    pr_diff: str,
    changed_files: list[str],
    *,
    backend: LLMBackend,
    fanout_threshold: int = DEFAULT_FANOUT_THRESHOLD,
    max_tokens: int = 4096,
    timeout_s: int = 720,
    memory_limit: str = "2G",
    cwd: Path | None = None,
) -> list[ReviewResult]:
    """Run the Reviewer phase.

    If ``len(changed_files) <= fanout_threshold``, runs a single LLM call
    covering the entire diff and returns a one-element list.

    If ``len(changed_files) > fanout_threshold``, shards by directory affinity:
    groups files by their top-level directory, runs one LLM call per shard,
    and returns one ``ReviewResult`` per shard with ``shard_id`` set to the
    directory name.

    Each LLM call is prompted with the shard's files + the full *pr_diff*
    (reviewers need global context).  The LLM must return ``ReviewResult``
    JSON.

    Returns ``list[ReviewResult]`` (always at least one element).
    Raises ``ValueError`` on JSON parse failure or schema validation error.
    """
    if len(changed_files) <= fanout_threshold:
        return [
            _call_backend(
                pr_number,
                changed_files,
                pr_diff,
                None,
                backend=backend,
                max_tokens=max_tokens,
                timeout_s=timeout_s,
                memory_limit=memory_limit,
                cwd=cwd,
            )
        ]

    shards = _shard_by_directory(changed_files)
    results: list[ReviewResult] = []
    for shard_id, shard_files in shards.items():
        results.append(
            _call_backend(
                pr_number,
                shard_files,
                pr_diff,
                shard_id,
                backend=backend,
                max_tokens=max_tokens,
                timeout_s=timeout_s,
                memory_limit=memory_limit,
                cwd=cwd,
            )
        )
    return results
