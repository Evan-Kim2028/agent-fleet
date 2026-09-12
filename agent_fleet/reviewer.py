"""Fleet Reviewer phase module.

Reads a PR diff and changed-file list, calls the LLM backend to produce a
ReviewResult.  When the number of changed files exceeds *fanout_threshold* the
diff is reviewed in per-top-level-directory shards so that each LLM call stays
within a manageable context window.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from agent_fleet.contracts.review import ReviewResult, ReviewVerdict, validate_review

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.hooks import LLMBackend, LLMSession

# Files-changed threshold above which reviewer shards into multiple LLM calls.
DEFAULT_FANOUT_THRESHOLD = 20

# Patterns identifying test files, inferred from this repo's own conventions
# (pytest `test_*.py` / `*_test.py`, `tests/` dirs) plus common JS/TS/other
# ecosystem conventions (`*.test.ts(x)`, `*.spec.ts`, `__tests__/`).
_TEST_FILE_PATTERNS: tuple[str, ...] = (
    r"(^|/)test_[^/]+\.py$",
    r"_test\.py$",
    r"\.test\.[cm]?[jt]sx?$",
    r"\.spec\.[cm]?[jt]sx?$",
    r"(^|/)tests?/",
    r"(^|/)__tests__/",
)

# Reason surfaced when a changeset is entirely test files. Kept as a module
# constant so callers/tests can assert on the exact wording.
TESTS_ONLY_REASON = "changeset contains only test files — no implementation change"


def is_tests_only_changeset(files: list[str]) -> bool:
    """True when *files* is non-empty and every path looks like a test file.

    Mirrors ``is_trivial_pr``'s shape (empty is NOT "tests-only" — that's the
    empty-changeset gate's job) but matches test-file conventions instead of
    docs/lock/asset conventions.
    """
    if not files:
        return False
    return all(any(re.search(pattern, f) for pattern in _TEST_FILE_PATTERNS) for f in files)


def _flag_tests_only_approvals(
    results: list[ReviewResult], changed_files: list[str]
) -> list[ReviewResult]:
    """Downgrade a bare APPROVE to REQUEST_CHANGES when the diff is tests-only.

    A tests-only changeset is often legitimate (added coverage, a test-only
    refactor, a regression test for an already-fixed bug), so this does not
    hard-fail the run — it reuses the existing REQUEST_CHANGES verdict /
    ``review_changes_requested`` outcome vocabulary so the run still lands in
    ``ok_outcomes`` and gets a draft/salvage-style path instead of a silent
    "completed" approval. Non-APPROVE verdicts (already BLOCK or
    REQUEST_CHANGES) are left untouched.
    """
    if not is_tests_only_changeset(changed_files):
        return results
    flagged: list[ReviewResult] = []
    for result in results:
        if result.verdict != ReviewVerdict.APPROVE:
            flagged.append(result)
            continue
        flag_issue = {"severity": "medium", "file": None, "message": TESTS_ONLY_REASON}
        issues = [*result.issues, flag_issue]
        flagged.append(
            ReviewResult(
                pr_number=result.pr_number,
                verdict=ReviewVerdict.REQUEST_CHANGES,
                summary=f"{TESTS_ONLY_REASON}. {result.summary}".strip(),
                issues=issues,
                shard_id=result.shard_id,
            )
        )
    return flagged


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


# Max characters kept from a goal/context/summary string embedded in the
# reviewer prompt. Keeps the prompt compact even if a caller passes a very
# long task description.
_PROMPT_FIELD_MAX_CHARS = 2000


def _truncate(text: str, limit: int = _PROMPT_FIELD_MAX_CHARS) -> str:
    """Truncate *text* to *limit* chars, appending a marker if cut."""
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[:limit].rstrip() + " …[truncated]"


def _build_prompt(
    pr_number: int,
    shard_files: list[str],
    pr_diff: str,
    shard_id: str | None,
    *,
    task_goal: str = "",
    task_context: str = "",
    implementation_summary: str = "",
) -> str:
    """Return the reviewer prompt for a single LLM call."""
    shard_note = (
        f"You are reviewing shard '{shard_id}' (files listed below)."
        if shard_id is not None
        else "You are reviewing the entire change set."
    )
    files_block = "\n".join(f"  - {f}" for f in shard_files)
    task_block = ""
    goal_stated = bool(task_goal.strip())
    if goal_stated:
        task_block = (
            f"\nOriginal task (what this run was SUPPOSED to accomplish):\n{_truncate(task_goal)}\n"
        )
    if task_context.strip():
        task_block += f"\nTask context:\n{_truncate(task_context)}\n"
    if implementation_summary.strip():
        task_block += f"\nImplementer summary:\n{_truncate(implementation_summary)}\n"

    goal_check = (
        "\nBefore verdicting, explicitly check whether the diff actually accomplishes "
        "the original task above. A diff that is off-task (touches unrelated files, "
        "implements something other than what was asked) or incomplete (e.g. adds only "
        "tests/docs without the requested behavior change) must NOT be approved — verdict "
        "it request_changes (or block for severe cases) and say why in summary/issues, even "
        "if the changes present are individually clean and low-risk.\n"
        if goal_stated
        else ""
    )

    return (
        f"You are a senior code reviewer for change set #{pr_number}.\n"
        f"{shard_note}\n\n"
        f"Files in scope for this review:\n{files_block}\n"
        f"{task_block}"
        f"{goal_check}\n"
        f"Diff:\n{pr_diff or '(no diff captured — review from changed files and summary)'}\n\n"
        "Return ONLY a JSON object with these fields:\n"
        "  pr_number   (integer) — the change set number above\n"
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
    task_goal: str = "",
    task_context: str = "",
    implementation_summary: str = "",
    model: str | None = None,
    allowed_tools: list[str] | None = None,
    session: LLMSession | None = None,
) -> ReviewResult:
    """Issue one LLM call and parse the result into a ReviewResult."""
    prompt = _build_prompt(
        pr_number,
        shard_files,
        pr_diff,
        shard_id,
        task_goal=task_goal,
        task_context=task_context,
        implementation_summary=implementation_summary,
    )
    if session is not None:
        result = session.send(
            prompt,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
            allowed_tools=allowed_tools,
        )
    else:
        result = backend.run(
            prompt,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
            memory_limit=memory_limit,
            allowed_tools=allowed_tools or [],
            cwd=cwd,
            model=model,
            mode="plan",
        )
    try:
        raw = _extract_json(result.stdout)
    except ValueError:
        # Some backends (e.g. grok) occasionally return prose or an empty
        # completion instead of the ReviewResult object. One strict retry
        # recovers these without failing the whole review phase.
        retry_prompt = (
            prompt + "\n\nIMPORTANT: Your previous response contained no parseable JSON. "
            "Respond with ONLY the ReviewResult JSON object — no prose, no "
            "markdown fences, no explanation."
        )
        if session is not None:
            result = session.send(
                retry_prompt,
                max_tokens=max_tokens,
                timeout_s=timeout_s,
                allowed_tools=allowed_tools,
            )
        else:
            result = backend.run(
                retry_prompt,
                max_tokens=max_tokens,
                timeout_s=timeout_s,
                memory_limit=memory_limit,
                allowed_tools=allowed_tools or [],
                cwd=cwd,
                model=model,
                mode="plan",
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


def aggregate_verdict(reviews: list[ReviewResult]) -> ReviewVerdict:
    """Return the strictest verdict across shard reviews."""
    priority = {
        ReviewVerdict.BLOCK: 3,
        ReviewVerdict.REQUEST_CHANGES: 2,
        ReviewVerdict.APPROVE: 1,
    }
    if not reviews:
        return ReviewVerdict.REQUEST_CHANGES
    return max(reviews, key=lambda review: priority[review.verdict]).verdict


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
    task_goal: str = "",
    task_context: str = "",
    implementation_summary: str = "",
    model: str | None = None,
    allowed_tools: list[str] | None = None,
    session: LLMSession | None = None,
    allow_tests_only_approval: bool = False,
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

    A tests-only changeset (every changed path matches a test-file
    convention) is a common, often-legitimate shape (new coverage, a
    test-only refactor, a regression test for an already-fixed bug) — but it
    is also exactly the shape of a run that never actually implemented the
    requested behavior change. By default, a bare APPROVE on a tests-only
    changeset is downgraded to REQUEST_CHANGES with a clear reason so it does
    not silently look identical to a real fix landing. Pass
    ``allow_tests_only_approval=True`` to opt out for callers/tasks where a
    tests-only PR is known to be the intended deliverable.

    Returns ``list[ReviewResult]`` (always at least one element).
    Raises ``ValueError`` on JSON parse failure or schema validation error.
    """
    if len(changed_files) <= fanout_threshold:
        results = [
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
                task_goal=task_goal,
                task_context=task_context,
                implementation_summary=implementation_summary,
                model=model,
                allowed_tools=allowed_tools,
                session=session,
            )
        ]
        if allow_tests_only_approval:
            return results
        return _flag_tests_only_approvals(results, changed_files)

    shards = _shard_by_directory(changed_files)
    results = []
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
                task_goal=task_goal,
                task_context=task_context,
                implementation_summary=implementation_summary,
                model=model,
                allowed_tools=allowed_tools,
                session=session,
            )
        )
    if allow_tests_only_approval:
        return results
    return _flag_tests_only_approvals(results, changed_files)
