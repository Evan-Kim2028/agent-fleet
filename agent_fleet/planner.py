"""Fleet Planner phase module.

Calls an LLM backend with a structured prompt requesting a TaskSpec JSON output,
validates the JSON against the task_spec schema, applies a mechanical cross-cutting
scope check, and returns a validated TaskSpec dataclass.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from agent_fleet.contracts.task_spec import (
    DecompositionDecision,
    RiskTier,
    Scope,
    TaskSpec,
    validate_task_spec,
)
from agent_fleet.spine_config import SpineConfig

if TYPE_CHECKING:
    from agent_fleet.hooks import LLMBackend, LLMSession, PersonaResolver

# JSON schema summary embedded in the prompt so the LLM knows what to produce.
_SCHEMA_SUMMARY = """\
Required fields (all must be present):
  issue_number          integer >= 1
  decomposition_decision  "single" | "decompose" | "rejected" | "program"
  cross_cutting_acknowledged  optional bool. Set true ONLY when you choose
                        "single" despite allowed_paths spanning persona
                        boundaries (e.g. backend/+frontend/) AND the work is
                        trivially small. Suppresses the mechanical decompose
                        override. Justify in decomposition_reason. Omit or
                        false otherwise.
  decomposition_reason  string
  child_issues_proposed array of {title: str, body: str, persona: str,
                                  allowed_paths?: [str, ...]}
                        (persona: one of the available personas listed above,
                        OR a novel lowercase hyphenated role name such as
                        "data-validator" or "infra-automation" which will be
                        synthesized on demand by PersonaFoundry)
                        allowed_paths scopes each sibling's edits when the
                        runner dispatches cooperative children.
  scope                 {allowed_paths: [str, ...], forbidden_paths: [str, ...]}
  research_plan         array of {id: str, question: str, scope_paths: [str], needs_browser: bool}
  acceptance_criteria   [str, ...]
  risk_tier             "low" | "medium" | "high"
  critical_paths_touched [str, ...]
  coordination_spec     null  OR  {merge_order?, schema_contracts_added?,
                                   schema_contracts_removed?, smoke_test_suggestion?,
                                   shared_branch?, interface_brief?}
                        interface_brief is REQUIRED whenever decomposition_decision
                        is "decompose" AND the children span persona boundaries
                        (e.g. backend+frontend or pipeline+api). Shape:
                          {kind: "http_route"|"parquet_schema"|"json_schema"|
                                 "function_signature",
                           route?: str, request_shape?: obj, response_shape?: obj,
                           fixture_path?: str, notes?: str}
                        The brief is the contract each sibling persona codes
                        against so they can land in parallel without waiting on
                        each other's PR.

When to choose "program":
  Choose "program" for tasks best solved by dynamically orchestrating many
  subagents with routing, fan-out, branching, and convergence that a static
  DECOMPOSE or DAG cannot express. When decomposition_decision is "program",
  place the Python source in the top-level "program" field. The program may
  call five injected primitives: agent(prompt, *, persona=None, context="",
  complexity=None, pipeline=None, allowed_paths=(), title=None, schema=None)
  dispatches one subagent and returns an AgentResult (.ok, .summary, .data);
  parallel(thunks) runs a list of callables concurrently and returns results
  in order; pipeline(items, *stages) passes each item through stages where
  each stage receives (prev, original, index); phase(title) marks a named
  execution phase; log(message) appends a message to the run log. The program
  ends with a top-level "return <final_answer>" and only that return value
  crosses back to the parent -- subagent transcripts remain isolated.
"""


def _is_cross_cutting(
    allowed_paths: list[str],
    cross_cutting_groups: tuple[frozenset[str], ...],
) -> bool:
    """Return True if allowed_paths spans multiple persona-boundary groups.

    Called AFTER the LLM emits its decision so we can override single → decompose.
    *cross_cutting_groups* is injected by the caller (from SpineConfig) so this
    function is pure and repo-neutral.
    """
    for group in cross_cutting_groups:
        prefixes_hit = sum(any(p.startswith(prefix) for p in allowed_paths) for prefix in group)
        if prefixes_hit >= 2:
            return True
    return False


def _extract_json(text: str) -> dict[str, Any]:
    """Extract the first JSON object from *text* (LLM may include surrounding prose).

    Strips markdown code fences before trying. Raises ValueError if no valid
    JSON object is found.
    """
    # Strip markdown code fences (```json ... ``` or ``` ... ```)
    fence_re = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
    fence_match = fence_re.search(text)
    if fence_match:
        candidate = fence_match.group(1)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass  # fall through to bare-JSON search

    # Search for a bare JSON object
    obj_match = re.search(r"\{.*\}", text, re.DOTALL)
    if obj_match:
        try:
            return json.loads(obj_match.group())
        except json.JSONDecodeError as exc:
            raise ValueError(f"Found JSON-like text but could not parse it: {exc}") from exc

    raise ValueError("No JSON object found in LLM output")


def _build_prompt(
    issue_number: int,
    issue_title: str,
    issue_body: str,
    *,
    persona_names: tuple[str, ...],
) -> str:
    """Construct the structured prompt sent to the LLM backend."""
    personas_line = ", ".join(persona_names) if persona_names else "(none configured)"
    return (
        f"You are the Planner phase of a software-agent fleet.\n"
        f"Analyse the task below and produce a TaskSpec JSON object.\n"
        f"Respond with ONLY the JSON — no prose before or after.\n\n"
        f"Available personas: {personas_line}\n\n"
        f"Task #{issue_number}: {issue_title}\n\n"
        f"{issue_body}\n\n"
        f"---\n"
        f"Expected JSON schema:\n{_SCHEMA_SUMMARY}"
    )


def plan(
    issue_number: int,
    issue_title: str,
    issue_body: str,
    *,
    backend: LLMBackend,
    persona_resolver: PersonaResolver,
    spine_config: SpineConfig | None = None,
    max_tokens: int = 4096,
    timeout_s: int = 720,
    memory_limit: str = "4G",
    max_retries: int = 2,
    session: LLMSession | None = None,
) -> TaskSpec:
    """Run the Planner phase.

    Calls the LLM with a structured prompt requesting a TaskSpec JSON output.
    Validates the JSON against the task_spec schema. Applies the mechanical
    scope-check fallback: if the LLM proposes decomposition_decision="single"
    but the allowed_paths span multiple persona boundaries, override to
    decomposition_decision="decompose" and set decomposition_reason accordingly.

    On JSON-parse or schema-validation failure, re-prompts the LLM up to
    ``max_retries`` times with the validator's error message appended so the
    model can self-correct (LLMs deterministically misformat structured fields
    like ``merge_order`` for some issue classes).

    Returns a validated TaskSpec dataclass.
    Raises ValueError after exhausting retries.
    """
    _spine = spine_config if spine_config is not None else SpineConfig.defaults()
    base_prompt = _build_prompt(
        issue_number,
        issue_title,
        issue_body,
        persona_names=tuple(sorted(persona_resolver.list_personas())),
    )
    last_error: str | None = None
    data: dict[str, Any] | None = None

    for attempt in range(max_retries + 1):
        prompt = (
            base_prompt
            if last_error is None
            else (
                f"{base_prompt}\n\n"
                f"---\n"
                f"Your previous output failed validation:\n{last_error}\n"
                f"Respond again with ONLY the corrected JSON."
            )
        )
        if session is not None:
            result = session.send(
                prompt,
                max_tokens=max_tokens,
                timeout_s=timeout_s,
                allowed_tools=[],
            )
        else:
            result = backend.run(
                prompt,
                max_tokens=max_tokens,
                timeout_s=timeout_s,
                memory_limit=memory_limit,
                allowed_tools=[],
            )

        # Distinguish backend failure from "model returned prose without JSON".
        # An exit_code != 0 OR empty stdout means the LLM call itself failed
        # (auth error, transport timeout, swallowed exception in
        # CursorSession.send, etc.). Retrying on that just produces the same
        # opaque failure; raise immediately with the underlying diagnostic
        # instead of masking it as "No JSON object found in LLM output".
        if result.exit_code != 0 or not result.stdout.strip():
            stderr_tail = (result.stderr or "")[-500:]
            cause = getattr(result, "cause", None)
            cause_str = f"; cause={type(cause).__name__}: {cause}" if cause else ""
            raise ValueError(
                f"PLAN backend call failed: exit_code={result.exit_code}, "
                f"stderr_tail={stderr_tail!r}{cause_str}"
            )

        try:
            data = _extract_json(result.stdout)
            validate_task_spec(data)
            break
        except ValueError as exc:
            last_error = str(exc)
        except Exception as exc:
            last_error = f"TaskSpec schema validation failed: {exc}"

        if attempt == max_retries:
            raise ValueError(last_error)

    assert data is not None  # loop guarantees this

    # Mechanical override: cross-cutting paths override "single" → "decompose",
    # UNLESS the LLM explicitly set ``cross_cutting_acknowledged: true`` in its
    # output. That field is the LLM's opt-out for cases where the change is
    # trivially small even though paths span persona boundaries (e.g. wiring up
    # a new response field already present in both api/ and frontend/).
    if (
        data.get("decomposition_decision") == DecompositionDecision.SINGLE.value
        and _is_cross_cutting(data["scope"]["allowed_paths"], _spine.cross_cutting_groups)
        and not data.get("cross_cutting_acknowledged", False)
    ):
        data["decomposition_decision"] = DecompositionDecision.DECOMPOSE.value
        original_reason = data.get("decomposition_reason", "")
        override_note = (
            "[mechanical override] allowed_paths span multiple persona boundaries; "
            "forced decompose."
        )
        data["decomposition_reason"] = (
            f"{original_reason} {override_note}".strip() if original_reason else override_note
        )

    return TaskSpec(
        issue_number=data["issue_number"],
        decomposition_decision=DecompositionDecision(data["decomposition_decision"]),
        decomposition_reason=data["decomposition_reason"],
        child_issues_proposed=list(data["child_issues_proposed"]),
        scope=Scope(
            allowed_paths=list(data["scope"]["allowed_paths"]),
            forbidden_paths=list(data["scope"]["forbidden_paths"]),
        ),
        research_plan=list(data["research_plan"]),
        acceptance_criteria=list(data["acceptance_criteria"]),
        risk_tier=RiskTier(data["risk_tier"]),
        critical_paths_touched=list(data["critical_paths_touched"]),
        coordination_spec=data["coordination_spec"],
        dag=data.get("dag"),
    )
