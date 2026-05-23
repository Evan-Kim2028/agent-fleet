"""Fleet Tech Lead phase module.

Content-triggered: invoked when risk_tier is HIGH, critical_paths_touched is
non-empty, or coordination_spec has a non-empty merge_order.

Returns a TechLeadReview that the FleetRunner uses to decide whether to
escalate to a human or proceed. Does NOT side-effect on any GitForge.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from agent_fleet.contracts.task_spec import RiskTier, TaskSpec
from agent_fleet.contracts.tech_lead_review import (
    TechLeadReview,
    TechLeadVerdict,
    validate_tech_lead_review,
)

if TYPE_CHECKING:
    from agent_fleet.contracts.review import ReviewResult
    from agent_fleet.hooks import LLMBackend

# ---------------------------------------------------------------------------
# Trigger guard
# ---------------------------------------------------------------------------


def _should_trigger(task_spec: TaskSpec, _reviews: list[ReviewResult]) -> bool:
    """Return True if the Tech Lead phase should run.

    Triggers when ANY of:
    - task_spec.risk_tier == RiskTier.HIGH
    - task_spec.critical_paths_touched is non-empty
    - task_spec.coordination_spec is not None and
      task_spec.coordination_spec.get("merge_order") is non-empty
    """
    if task_spec.risk_tier == RiskTier.HIGH:
        return True

    if task_spec.critical_paths_touched:
        return True

    return bool(
        task_spec.coordination_spec is not None
        and task_spec.coordination_spec.get("merge_order")
    )


# Expose the same name the plan spec references as the public API.
should_invoke_tech_lead = _should_trigger


# ---------------------------------------------------------------------------
# JSON extraction (same semantics as planner._extract_json)
# ---------------------------------------------------------------------------


def _extract_json(text: str) -> dict[str, Any]:
    """Extract the first JSON object from *text*. Same semantics as planner._extract_json."""
    fence_re = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
    fence_match = fence_re.search(text)
    if fence_match:
        candidate = fence_match.group(1)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass  # fall through to bare-JSON search

    obj_match = re.search(r"\{.*\}", text, re.DOTALL)
    if obj_match:
        try:
            return json.loads(obj_match.group())
        except json.JSONDecodeError as exc:
            raise ValueError(f"Found JSON-like text but could not parse it: {exc}") from exc

    raise ValueError("No JSON object found in LLM output")


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def _build_prompt(
    task_spec: TaskSpec,
    reviews: list[ReviewResult],
    pr_number: int,
) -> str:
    """Build the Tech Lead prompt from task_spec and review summaries."""
    task_spec_json = json.dumps(task_spec.to_dict(), indent=2)

    review_summaries = "\n".join(
        f"- Review {i + 1} (PR #{r.pr_number}, verdict={r.verdict.value}): {r.summary}"
        for i, r in enumerate(reviews)
    )

    return (
        "You are the Tech Lead phase of a software-agent fleet.\n"
        "Your job is to perform a senior architectural review of the task plan and\n"
        "all implementation reviews, checking for:\n"
        "  1. Cross-PR integration concerns (e.g. API contract breakage between PRs)\n"
        "  2. Schema breakage or backwards-incompatible changes\n"
        "  3. Deploy ordering issues (e.g. database migration must land before API change)\n"
        "  4. Disagreements with Planner scope or decomposition decisions\n\n"
        "If you disagree with the Planner's decomposition or scope decision, "
        "set verdict='escalate'.\n"
        "If any concern is severe enough to block shipping, set verdict='block'.\n"
        "If everything looks acceptable, set verdict='approve'.\n\n"
        f"PR under review: #{pr_number}\n\n"
        f"## Original TaskSpec (risk_tier={task_spec.risk_tier.value})\n\n"
        f"```json\n{task_spec_json}\n```\n\n"
        f"## Review Summaries\n\n"
        f"{review_summaries if review_summaries else '(no reviews provided)'}\n\n"
        "---\n"
        "Respond with ONLY a JSON object matching this schema:\n"
        "{\n"
        '  "pr_number": <integer — must equal the PR number above>,\n'
        '  "verdict": "approve" | "block" | "escalate",\n'
        '  "summary": <string — concise overall assessment>,\n'
        '  "escalation_required": <boolean — true iff verdict == "escalate">,\n'
        '  "disagreement_with_planner": <string or null>,\n'
        '  "cross_pr_concerns": [<string>, ...]\n'
        "}\n"
    )


# ---------------------------------------------------------------------------
# Public phase function
# ---------------------------------------------------------------------------


def tech_lead_review(
    task_spec: TaskSpec,
    reviews: list[ReviewResult],
    pr_number: int,
    *,
    backend: LLMBackend,
    max_tokens: int = 4096,
    timeout_s: int = 720,
    memory_limit: str = "4G",
) -> TechLeadReview | None:
    """Run the Tech Lead phase (content-triggered).

    If _should_trigger() returns False, returns None immediately (no LLM call).

    Otherwise, prompts the LLM with: the original TaskSpec, all ReviewResult
    summaries, and instructions to check for cross-PR integration concerns,
    schema breakage, deploy ordering, and disagreements with Planner scope.

    The LLM must return TechLeadReview JSON. If verdict == "escalate", the
    caller (FleetRunner) is responsible for posting a human-escalation comment;
    tech_lead_review() does not side-effect on GitForge.

    Returns TechLeadReview or None.
    Raises ValueError on JSON parse failure or schema validation error.
    """
    if not _should_trigger(task_spec, reviews):
        return None

    prompt = _build_prompt(task_spec, reviews, pr_number)
    result = backend.run(
        prompt,
        max_tokens=max_tokens,
        timeout_s=timeout_s,
        memory_limit=memory_limit,
        allowed_tools=[],
    )

    data = _extract_json(result.stdout)

    try:
        validate_tech_lead_review(data)
    except Exception as exc:
        raise ValueError(f"TechLeadReview schema validation failed: {exc}") from exc

    return TechLeadReview(
        pr_number=data["pr_number"],
        verdict=TechLeadVerdict(data["verdict"]),
        summary=data["summary"],
        escalation_required=data["escalation_required"],
        disagreement_with_planner=data["disagreement_with_planner"],
        cross_pr_concerns=list(data["cross_pr_concerns"]),
    )
