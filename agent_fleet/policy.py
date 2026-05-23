"""Optional policy seam for merge/review gates (not wired into LocalFleetRunner yet)."""

from __future__ import annotations

import enum
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from agent_fleet.contracts.design_review import DesignReview, DesignVerdict
from agent_fleet.contracts.review import ReviewResult
from agent_fleet.contracts.task_spec import RiskTier, TaskSpec
from agent_fleet.contracts.verify_result import VerifyResult, VerifySeverity
from agent_fleet.hooks import Persona
from agent_fleet.spine_config import SpineConfig


# ---------------------------------------------------------------------------
# Decision enums
# ---------------------------------------------------------------------------


class MergeDecision(str, enum.Enum):
    """Possible outcomes of the merge-gate check."""

    ALLOW = "allow"
    BLOCK = "block"
    NEEDS_HUMAN = "needs_human"


class EscalationTier(str, enum.Enum):
    """Which tier should receive an escalation, or none at all."""

    NONE = "none"
    TECH_LEAD = "tech_lead"
    HUMAN = "human"


class Action(str, enum.Enum):
    """How the runner should react to a verify result."""

    OK = "ok"
    RETRY = "retry"
    FATAL = "fatal"


# ---------------------------------------------------------------------------
# Decision log record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyDecision:
    """One structured record emitted per Policy call.

    Appended as a JSON line to the per-run decision log by the runner.
    ``inputs_hash`` is a short SHA-256 of the serialised inputs so the record
    stays compact while remaining reproducible for forensics.
    """

    phase: str
    decision_type: str  # "merge" | "escalation" | "scope_violation" | "verify_action"
    decision: str       # the enum value string
    inputs_hash: str    # 8-char hex prefix of sha256(json(inputs))
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "decision_type": self.decision_type,
            "decision": self.decision,
            "inputs_hash": self.inputs_hash,
            "rationale": self.rationale,
        }


def _hash_inputs(inputs: Any) -> str:
    """Return an 8-char hex prefix of sha256(json(inputs)).

    ``inputs`` must be JSON-serialisable.  Non-serialisable inputs fall back
    to a hash of their repr.
    """
    try:
        raw = json.dumps(inputs, sort_keys=True, default=str)
    except Exception:
        raw = repr(inputs)
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Context bags (thin wrappers so the Protocol methods stay ergonomic)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MergeContext:
    """All inputs the merge_decision() method needs."""

    ci_green: bool
    kimi_risk: str | None                   # "LOW" | "MEDIUM" | "HIGH" | "CRITICAL" | None
    out_of_scope_files: list[str]
    # Future-proofing: tiered gate on/off, cooldown, human-review paths
    tiered_gate_enabled: bool = False
    human_review_paths: tuple[str, ...] = ()
    cooldown_active: bool = False
    changed_files: Sequence[str] = ()


# ---------------------------------------------------------------------------
# Policy Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Policy(Protocol):
    """Injectable governance protocol for the fleet engine.

    All four methods receive their full input context and return a typed
    decision.  The default implementation (``DefaultPolicy``) mirrors the
    pre-extraction inline logic exactly.

    Implementations:
    - MUST be deterministic for a given input (the engine may call the same
      method multiple times with identical inputs).
    - SHOULD document departures from DefaultPolicy behaviour.
    - MUST NOT have side-effects (no forge calls, no filesystem writes).
    """

    def merge_decision(
        self,
        ctx: MergeContext,
        *,
        phase: str = "MERGE",
    ) -> tuple[MergeDecision, PolicyDecision]:
        """Decide whether to allow / block / escalate to human for a merge.

        Returns (decision, record) so callers can both act and log atomically.
        """
        ...

    def escalation_target(
        self,
        task_spec: TaskSpec,
        reviews: list[ReviewResult],
        *,
        phase: str = "REVIEW",
    ) -> tuple[EscalationTier, PolicyDecision]:
        """Decide whether tech_lead or human escalation is needed (or neither).

        Encapsulates: HIGH risk, critical_paths_touched, merge_order trigger.
        Returns (tier, record).
        """
        ...

    def scope_violation(
        self,
        persona: Persona,
        changed_files: list[str],
        *,
        phase: str = "VERIFY",
    ) -> tuple[tuple[str, ...], PolicyDecision]:
        """Return files outside the persona's scope, plus a log record.

        Returns (violating_files, record).  Empty tuple = no violation.
        """
        ...

    def verify_severity_action(
        self,
        verify_result: VerifyResult,
        *,
        phase: str = "VERIFY",
    ) -> tuple[Action, PolicyDecision]:
        """Map a VerifyResult severity to a runner action.

        OK → Action.OK, RETRY → Action.RETRY, FATAL → Action.FATAL.
        Returns (action, record).
        """
        ...

    def design_gate(
        self,
        design_review: DesignReview,
        *,
        phase: str = "DESIGN_REVIEW",
    ) -> tuple[Action, PolicyDecision]:
        """Decide whether a DesignReview result should block, warn, or pass.

        Maps DesignReview.verdict → Action:
          pass       → Action.OK          (no issue)
          needs_work → Action.RETRY       (advisory; runner may log but not block
                                           unless enforced — feeds existing remediation path)
          block      → Action.FATAL       (hard gate; runner blocks PR promotion)

        In policy_dry_run mode the runner logs "would have X" but does NOT
        enforce the action — the same ``_apply_policy_decision`` pattern used
        by other policy methods.

        The score threshold from ``SpineConfig.design_score_threshold`` is also
        checked: if the mean score across all dimensions falls below the
        threshold and the verdict is ``pass``, the action is upgraded to
        ``RETRY`` (advisory) so the implementer is notified even when the
        vision model returned an overly-generous verdict.

        Returns (action, record).
        """
        ...


# ---------------------------------------------------------------------------
# DefaultPolicy — mirrors pre-extraction inline logic exactly
# ---------------------------------------------------------------------------


_BLOCKING_RISK = frozenset({"MEDIUM", "HIGH", "CRITICAL"})
_SEVERITY_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


@dataclass(frozen=True)
class DefaultPolicy:
    """Concrete Policy that reproduces pre-extraction FleetRunner behaviour.

    All four methods are pure — no I/O, no side-effects.  Constructed from
    SpineConfig so it inherits the same allowlist / critical-prefix values.

    Invariant: ``DefaultPolicy(spine).X(inputs) == <old inline logic>(inputs)``
    for all X and all representative inputs.
    """

    spine: SpineConfig

    # ------------------------------------------------------------------
    # merge_decision
    # ------------------------------------------------------------------

    def merge_decision(
        self,
        ctx: MergeContext,
        *,
        phase: str = "MERGE",
    ) -> tuple[MergeDecision, PolicyDecision]:
        """Mirror tiered_merge_allowed() + park_pr_for_human_review() logic.

        Pre-extraction logic (agents/agents/merge_gate.py):
          Allowed iff CI green AND Kimi risk None/LOW AND zero out-of-scope.
          When tiered gate is disabled (default) → always ALLOW (legacy path).
          Human-review paths and cooldown are evaluated when gate is enabled.
        """
        inputs = {
            "ci_green": ctx.ci_green,
            "kimi_risk": ctx.kimi_risk,
            "out_of_scope_files": list(ctx.out_of_scope_files),
            "tiered_gate_enabled": ctx.tiered_gate_enabled,
            "cooldown_active": ctx.cooldown_active,
            "changed_files": list(ctx.changed_files),
        }
        h = _hash_inputs(inputs)

        # tiered gate is default-off; when off the legacy path never blocks.
        if not ctx.tiered_gate_enabled:
            rec = PolicyDecision(
                phase=phase,
                decision_type="merge",
                decision=MergeDecision.ALLOW.value,
                inputs_hash=h,
                rationale="tiered_gate_enabled=False; legacy merge path (always allow)",
            )
            return MergeDecision.ALLOW, rec

        # Check human-review paths: any changed file under a protected path
        # forces NEEDS_HUMAN regardless of risk/scope.
        if ctx.human_review_paths:
            for f in ctx.changed_files:
                for prefix in ctx.human_review_paths:
                    if f.startswith(prefix):
                        rec = PolicyDecision(
                            phase=phase,
                            decision_type="merge",
                            decision=MergeDecision.NEEDS_HUMAN.value,
                            inputs_hash=h,
                            rationale=f"human_review_paths match: {f!r} starts with {prefix!r}",
                        )
                        return MergeDecision.NEEDS_HUMAN, rec

        if ctx.cooldown_active:
            rec = PolicyDecision(
                phase=phase,
                decision_type="merge",
                decision=MergeDecision.BLOCK.value,
                inputs_hash=h,
                rationale="merge cooldown is active",
            )
            return MergeDecision.BLOCK, rec

        reasons: list[str] = []
        if not ctx.ci_green:
            reasons.append("CI not green")
        if ctx.kimi_risk is not None and ctx.kimi_risk.upper() in _BLOCKING_RISK:
            reasons.append(f"Kimi risk {ctx.kimi_risk.upper()}")
        if ctx.out_of_scope_files:
            reasons.append("out-of-scope files: " + ", ".join(ctx.out_of_scope_files))

        if reasons:
            rec = PolicyDecision(
                phase=phase,
                decision_type="merge",
                decision=MergeDecision.NEEDS_HUMAN.value,
                inputs_hash=h,
                rationale="; ".join(reasons),
            )
            return MergeDecision.NEEDS_HUMAN, rec

        rec = PolicyDecision(
            phase=phase,
            decision_type="merge",
            decision=MergeDecision.ALLOW.value,
            inputs_hash=h,
            rationale="CI green, risk acceptable, no out-of-scope files",
        )
        return MergeDecision.ALLOW, rec

    # ------------------------------------------------------------------
    # escalation_target
    # ------------------------------------------------------------------

    def escalation_target(
        self,
        task_spec: TaskSpec,
        reviews: list[ReviewResult],
        *,
        phase: str = "REVIEW",
    ) -> tuple[EscalationTier, PolicyDecision]:
        """Mirror tech_lead._should_trigger() logic.

        Triggers TECH_LEAD (which may itself escalate to HUMAN) when ANY of:
        - task_spec.risk_tier == HIGH
        - task_spec.critical_paths_touched non-empty
        - task_spec.coordination_spec has non-empty merge_order

        Otherwise NONE.

        This encodes the *content-triggered* condition.  The TechLead LLM
        response itself may produce a HUMAN verdict; that is handled separately
        in the runner (and in DefaultPolicy.merge_decision when the TechLeadReview
        has verdict == ESCALATE).
        """
        inputs = {
            "risk_tier": task_spec.risk_tier.value,
            "critical_paths_touched": task_spec.critical_paths_touched,
            "coordination_spec": task_spec.coordination_spec,
            "review_verdicts": [r.verdict.value for r in reviews],
        }
        h = _hash_inputs(inputs)

        if task_spec.risk_tier == RiskTier.HIGH:
            rec = PolicyDecision(
                phase=phase,
                decision_type="escalation",
                decision=EscalationTier.TECH_LEAD.value,
                inputs_hash=h,
                rationale="risk_tier=HIGH",
            )
            return EscalationTier.TECH_LEAD, rec

        if task_spec.critical_paths_touched:
            rec = PolicyDecision(
                phase=phase,
                decision_type="escalation",
                decision=EscalationTier.TECH_LEAD.value,
                inputs_hash=h,
                rationale=f"critical_paths_touched={task_spec.critical_paths_touched!r}",
            )
            return EscalationTier.TECH_LEAD, rec

        if (
            task_spec.coordination_spec is not None
            and task_spec.coordination_spec.get("merge_order")
        ):
            rec = PolicyDecision(
                phase=phase,
                decision_type="escalation",
                decision=EscalationTier.TECH_LEAD.value,
                inputs_hash=h,
                rationale="coordination_spec.merge_order is non-empty",
            )
            return EscalationTier.TECH_LEAD, rec

        rec = PolicyDecision(
            phase=phase,
            decision_type="escalation",
            decision=EscalationTier.NONE.value,
            inputs_hash=h,
            rationale="no escalation trigger (LOW/MEDIUM risk, no critical paths, no merge order)",
        )
        return EscalationTier.NONE, rec

    # ------------------------------------------------------------------
    # scope_violation
    # ------------------------------------------------------------------

    def scope_violation(
        self,
        persona: Persona,
        changed_files: list[str],
        *,
        phase: str = "VERIFY",
    ) -> tuple[tuple[str, ...], PolicyDecision]:
        """Return files outside the persona's allowed_paths.

        Mirrors merge_gate.out_of_scope_files() logic.  Empty allowed_paths
        means unrestricted (all files OK).
        """
        inputs = {
            "persona_name": persona.name,
            "allowed_paths": list(persona.allowed_paths),
            "changed_files": list(changed_files),
        }
        h = _hash_inputs(inputs)

        allowed = persona.allowed_paths
        if not allowed:
            rec = PolicyDecision(
                phase=phase,
                decision_type="scope_violation",
                decision="none",
                inputs_hash=h,
                rationale="persona has unrestricted scope (empty allowed_paths)",
            )
            return (), rec

        violating = tuple(
            f for f in changed_files
            if not any(f.startswith(prefix) for prefix in allowed)
        )
        if violating:
            rec = PolicyDecision(
                phase=phase,
                decision_type="scope_violation",
                decision="violation",
                inputs_hash=h,
                rationale=f"files outside allowed prefixes {allowed!r}: {violating!r}",
            )
        else:
            rec = PolicyDecision(
                phase=phase,
                decision_type="scope_violation",
                decision="none",
                inputs_hash=h,
                rationale="all changed files within allowed paths",
            )
        return violating, rec

    # ------------------------------------------------------------------
    # design_gate
    # ------------------------------------------------------------------

    def design_gate(
        self,
        design_review: DesignReview,
        *,
        phase: str = "DESIGN_REVIEW",
    ) -> tuple[Action, PolicyDecision]:
        """Map a DesignReview result to a runner Action.

        Logic:
          1. If verdict == block              → Action.FATAL  (hard gate)
          2. If verdict == needs_work         → Action.RETRY  (advisory; feeds
                                                remediation path)
          3. If verdict == pass BUT mean score < threshold → Action.RETRY
             (score-based advisory: vision model too generous)
          4. Otherwise                        → Action.OK

        In policy_dry_run mode the runner logs "would have X" but does not
        enforce the action (uses the existing _apply_policy_decision helper).
        """
        score_values = list(design_review.scores.values())
        mean_score = sum(score_values) / len(score_values) if score_values else 100.0
        threshold = self.spine.design_score_threshold

        inputs = {
            "verdict": design_review.verdict.value,
            "mean_score": mean_score,
            "threshold": threshold,
            "issue_count": len(design_review.issues),
        }
        h = _hash_inputs(inputs)

        if design_review.verdict == DesignVerdict.BLOCK:
            action = Action.FATAL
            rationale = (
                f"design verdict=block (mean_score={mean_score:.1f}, "
                f"threshold={threshold}, issues={len(design_review.issues)})"
            )
        elif design_review.verdict == DesignVerdict.NEEDS_WORK:
            action = Action.RETRY
            rationale = (
                f"design verdict=needs_work (mean_score={mean_score:.1f}, "
                f"issues={len(design_review.issues)})"
            )
        elif score_values and mean_score < threshold:
            # Verdict claimed pass but scores are below threshold — advisory.
            action = Action.RETRY
            rationale = (
                f"design verdict=pass but mean_score={mean_score:.1f} < "
                f"threshold={threshold} — advisory needs_work"
            )
        else:
            action = Action.OK
            rationale = (
                f"design verdict=pass, mean_score={mean_score:.1f} >= "
                f"threshold={threshold}"
            )

        rec = PolicyDecision(
            phase=phase,
            decision_type="design_gate",
            decision=action.value,
            inputs_hash=h,
            rationale=rationale,
        )
        return action, rec

    # ------------------------------------------------------------------
    # verify_severity_action
    # ------------------------------------------------------------------

    def verify_severity_action(
        self,
        verify_result: VerifyResult,
        *,
        phase: str = "VERIFY",
    ) -> tuple[Action, PolicyDecision]:
        """Map VerifySeverity to Action.

        OK → Action.OK, RETRY → Action.RETRY, FATAL → Action.FATAL.
        Mirrors the pre-extraction if/elif chain in FleetRunner._verify_loop.
        """
        inputs = {
            "severity": verify_result.severity.value,
            "message": verify_result.message,
        }
        h = _hash_inputs(inputs)

        if verify_result.severity == VerifySeverity.OK:
            action = Action.OK
            rationale = "verify passed"
        elif verify_result.severity == VerifySeverity.FATAL:
            action = Action.FATAL
            rationale = f"FATAL verify: {verify_result.message}"
        else:
            # RETRY
            action = Action.RETRY
            rationale = f"RETRY verify: {verify_result.message}"

        rec = PolicyDecision(
            phase=phase,
            decision_type="verify_action",
            decision=action.value,
            inputs_hash=h,
            rationale=rationale,
        )
        return action, rec
