"""propose.py — LLM-backed change proposer for the self-improvement loop.

Takes ONE top failure signature + its sample traces + the current content of
the target file, makes a SINGLE ``LLMBackend.run()`` call asking for a root-
cause hypothesis and a minimal unified-diff edit, and returns a structured
:class:`ChangeProposal`.

Security constraints
--------------------
* The proposer prompt contains ONLY structured signature data + raw traces
  from the run log.  It NEVER receives free-text advice authored by prior
  agent runs (no re-injection of previous proposals, no eval set contents).
* Proposed diffs are validated to touch exactly one file.
* The target file must pass :func:`guard.is_allowed` before the LLM is called.

Diff format expected from the LLM
----------------------------------
The LLM must return a fenced code block containing a standard unified diff::

    ```diff
    --- a/agents/personas/backend.md
    +++ b/agents/personas/backend.md
    @@ -10,6 +10,7 @@
    ...
    ```

Any text outside the fenced block is treated as the rationale.
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from agent_fleet.hooks import LLMBackend
from silphco.selfimprove.guard import _normalise, is_allowed
from silphco.selfimprove.mine import FailureSignature, TraceRecord


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChangeProposal:
    """Structured output of the proposer step.

    Attributes:
        signature: The failure signature that triggered this proposal.
        target_file: Relative path to the single file to be edited
            (validated by :func:`guard.is_allowed`).
        rationale: Root-cause hypothesis from the LLM (plain text).
        diff: Unified diff string (validated, touches exactly one file).
        raw_llm_output: Full LLM response before parsing (for debugging).
    """

    signature: FailureSignature
    target_file: str
    rationale: str
    diff: str
    raw_llm_output: str


class ProposerError(Exception):
    """Raised when the proposer cannot produce a valid :class:`ChangeProposal`."""


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_MAX_TRACES = 5
_MAX_TRACE_DETAIL_CHARS = 500
_MAX_FILE_CONTENT_CHARS = 8_000

_SYSTEM_PROMPT = """\
You are the SilphCo Agent Fleet self-improvement assistant.
Your task is to analyse a recurring failure pattern in the agent fleet and
propose a minimal, targeted fix to exactly ONE prompt/persona file.

Rules you MUST follow:
- Respond with a root-cause hypothesis (2-4 sentences) followed by a unified
  diff of the proposed change.
- The diff MUST be enclosed in a fenced code block marked ```diff ... ```.
- The diff MUST touch exactly ONE file.
- The change MUST be minimal — add or remove at most 20 lines.
- Do NOT change file names, import paths, or any Python/TOML/YAML syntax.
- Do NOT reference internal eval data, prior proposals, or your own chain of
  thought in the diff.
- If you cannot identify a safe, targeted fix, respond with:
  NO_SAFE_FIX: <one sentence explanation>
"""

def _format_traces(traces: Sequence[TraceRecord]) -> str:
    parts: list[str] = []
    for i, t in enumerate(traces[:_MAX_TRACES], 1):
        detail = (t.detail or "")[:_MAX_TRACE_DETAIL_CHARS]
        dur = f"{t.duration_s:.1f}s" if t.duration_s is not None else "?"
        parts.append(
            f"  [{i}] ts={t.ts} persona={t.persona} phase={t.phase} "
            f"duration={dur}\n      detail: {detail}"
        )
    return "\n".join(parts)


def _build_prompt(
    signature: FailureSignature,
    traces: Sequence[TraceRecord],
    target_file: str,
    file_content: str,
) -> str:
    trace_block = _format_traces(traces)
    content_snippet = file_content[:_MAX_FILE_CONTENT_CHARS]
    if len(file_content) > _MAX_FILE_CONTENT_CHARS:
        content_snippet += "\n... (truncated)"

    return textwrap.dedent(f"""\
        {_SYSTEM_PROMPT}

        ## Failure signature
        - persona:     {signature.persona}
        - phase:       {signature.phase}
        - error_class: {signature.error_class.value}

        ## Sample failure traces ({len(traces)} occurrences; showing up to {_MAX_TRACES})
        {trace_block}

        ## Target file to fix: {target_file}
        ```
        {content_snippet}
        ```

        Provide your root-cause hypothesis and the minimal unified diff now.
    """)


# ---------------------------------------------------------------------------
# Diff validation
# ---------------------------------------------------------------------------

_DIFF_FENCE_RE = re.compile(
    r"```diff\s*\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)

_UNIFIED_HEADER_RE = re.compile(r"^(?:---|\+\+\+)\s+(\S+)", re.MULTILINE)


def _extract_diff(llm_output: str) -> str | None:
    """Extract the unified diff from a fenced ```diff block.

    Returns the diff string (including the ``---``/``+++`` headers) or None
    if no fenced diff is found.
    """
    match = _DIFF_FENCE_RE.search(llm_output)
    if not match:
        return None
    return match.group(1).strip()


def _diff_target_files(diff: str) -> list[str]:
    """Return the list of ``b/`` paths mentioned in ``+++`` headers."""
    targets: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++ "):
            # Strip leading b/ or a/ prefix if present
            path = line[4:].strip()
            path = re.sub(r"^[ab]/", "", path)
            if path and path != "/dev/null":
                targets.append(path)
    return targets


def _validate_diff(diff: str, expected_target: str) -> str:
    """Validate that *diff* is parseable and touches exactly one allowed file.

    Args:
        diff: The unified diff string.
        expected_target: The file we expect the diff to touch.

    Returns:
        The validated diff (possibly normalised).

    Raises:
        :exc:`ProposerError`: If validation fails.
    """
    if not diff.strip():
        raise ProposerError("LLM returned an empty diff.")

    targets = _diff_target_files(diff)
    if len(targets) == 0:
        raise ProposerError(
            "Diff contains no +++ header — cannot verify target file."
        )
    if len(targets) > 1:
        raise ProposerError(
            f"Diff touches {len(targets)} files ({targets!r}); must touch exactly 1."
        )

    actual = targets[0]
    # Normalise both sides (strip a/b/ git-diff prefixes, reject traversal /
    # absolute paths) and require *exact* equality — endswith() was vulnerable
    # to prefix-injection attacks like "b/foo/agents/personas/backend.md"
    # matching expected "agents/personas/backend.md".
    norm_actual = _normalise(actual)
    norm_expected = _normalise(expected_target)
    if norm_actual is None:
        raise ProposerError(
            f"Diff target {actual!r} contains path traversal or is absolute — rejected."
        )
    if norm_expected is None:
        raise ProposerError(
            f"Expected target {expected_target!r} contains path traversal or is absolute — rejected."
        )
    if norm_actual != norm_expected:
        raise ProposerError(
            f"Diff target {actual!r} does not match expected target {expected_target!r}."
        )

    if not is_allowed(expected_target):
        raise ProposerError(
            f"Proposed target {expected_target!r} is not permitted by the guard."
        )

    return diff


def _extract_rationale(llm_output: str) -> str:
    """Extract the rationale text (everything outside the diff fence)."""
    # Remove the fenced diff block
    without_diff = _DIFF_FENCE_RE.sub("", llm_output).strip()
    # Trim to a reasonable length
    return without_diff[:2000]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def propose(
    signature: FailureSignature,
    traces: Sequence[TraceRecord],
    target_file: str,
    *,
    backend: LLMBackend,
    repo_root: Path,
    max_tokens: int = 2048,
    timeout_s: int = 120,
) -> ChangeProposal:
    """Generate a :class:`ChangeProposal` for the given failure signature.

    Makes exactly ONE call to *backend*.  The proposer prompt contains only
    structured data from the run log — never eval corpus content or prior
    agent free-text.

    Args:
        signature: The failure signature to address.
        traces: Sample failure trace records (from the mining step).
        target_file: Relative path (repo-root-relative) to the file to edit.
            Must pass :func:`guard.is_allowed`.
        backend: LLM backend to call.
        repo_root: Absolute path to the repository root.
        max_tokens: Token budget for the LLM call.
        timeout_s: Timeout for the LLM call in seconds.

    Returns:
        A validated :class:`ChangeProposal`.

    Raises:
        :exc:`ProposerError`: When the guard denies the target, the LLM call
            fails, or the response cannot be parsed/validated.
    """
    # Guard check before any LLM call.
    if not is_allowed(target_file):
        raise ProposerError(
            f"Target file {target_file!r} is not permitted by guard.is_allowed()."
        )

    # Read current file content.
    abs_path = repo_root / target_file
    try:
        file_content = abs_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProposerError(f"Cannot read target file {abs_path}: {exc}") from exc

    prompt = _build_prompt(signature, traces, target_file, file_content)

    result = backend.run(
        prompt,
        max_tokens=max_tokens,
        timeout_s=timeout_s,
        memory_limit="2G",
        allowed_tools=[],
        cwd=None,
    )

    if result.exit_code != 0:
        raise ProposerError(
            f"LLM backend returned exit_code={result.exit_code}: {result.stderr[:500]}"
        )

    llm_output = result.stdout

    # Check for explicit no-fix signal.
    if llm_output.strip().startswith("NO_SAFE_FIX:"):
        reason = llm_output.strip().removeprefix("NO_SAFE_FIX:").strip()
        raise ProposerError(f"LLM declined to propose a fix: {reason}")

    diff = _extract_diff(llm_output)
    if diff is None:
        raise ProposerError(
            "LLM response does not contain a fenced ```diff block."
        )

    validated_diff = _validate_diff(diff, target_file)
    rationale = _extract_rationale(llm_output)

    return ChangeProposal(
        signature=signature,
        target_file=target_file,
        rationale=rationale,
        diff=validated_diff,
        raw_llm_output=llm_output,
    )
