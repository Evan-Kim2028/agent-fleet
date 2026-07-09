"""Skill synthesis engine for the self-improving flywheel.

This is the core of the "agent orchestrator updates skills" capability.

Design goals:
- Operates on the central ~/.agent-fleet/ store (cross-repo)
- Can be triggered by the dispatcher / orchestrator (not only CLI)
- Produces high-quality candidate skills for the _fleet tier
- Reuses the existing level_up gate + promotion machinery
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_fleet.learning.llm_synthesis import get_synthesis_context
from agent_fleet.level_up.paths import FLEET_TIER, LEVEL_UP_ROOT
from agent_fleet.level_up.train import train_persona

logger = logging.getLogger(__name__)

_FLEET_LEARNER_PERSONA_PATH = (
    Path(__file__).resolve().parent.parent / "personas" / "fleet-learner.md"
)


@dataclass
class FleetSynthesisResult:
    personas_updated: list[str]
    new_rules_proposed: int
    promoted_to_fleet: int
    details: dict[str, Any]


def synthesize_fleet_skills(
    *,
    personas: list[str] | None = None,
    min_experience_rows: int = 20,
    dry_run: bool = False,
    backend: Any = None,  # noqa: ANN401
    resolver: Any = None,  # noqa: ANN401, ARG001
    fleet_config: Any = None,  # noqa: ANN401, ARG001
) -> FleetSynthesisResult:
    """
    Run cross-repo skill synthesis for the global fleet tier.

    This is intended to be callable from:
    - CLI (agent-fleet learn)
    - Dispatcher / background maintenance loop
    - A special "fleet-learner" persona run

    Currently this is a thin wrapper that leverages the per-persona
    train_persona machinery but biases toward contributing to _fleet.
    """
    if personas is None:
        # Default personas that make sense to evolve at fleet level
        personas = ["coder", "reviewer", "pr-analyzer"]

    updated: list[str] = []
    total_proposed = 0
    total_promoted = 0

    for persona in personas:
        # Also look across all repo keys for this persona
        total_rows = 0
        for repo_dir in LEVEL_UP_ROOT.iterdir():
            if repo_dir.name == FLEET_TIER:
                continue
            exp_file = repo_dir / persona / "experience.jsonl"
            if exp_file.exists():
                lines = [line for line in exp_file.read_text().splitlines() if line.strip()]
                total_rows += len(lines)

        if total_rows < min_experience_rows:
            continue

        # === Real LLM synthesis path ===
        # Dispatch the fleet-learner persona against ~/.agent-fleet/ to extract
        # generalizable skills from accumulated experience. Persist the synthesized
        # learning to _fleet/<persona>/learnings/<ts>.md (human-readable) and
        # skills_queue.jsonl (machine-readable, future-promotable).
        if backend is not None:
            try:
                context = get_synthesis_context(persona, max_rows=120)
                synthesized = _run_llm_synthesis(
                    backend=backend,
                    persona=persona,
                    context=context,
                )
                if synthesized:
                    written = _persist_learning(
                        persona=persona,
                        skills=synthesized,
                        context=context,
                        dry_run=dry_run,
                    )
                    if written:
                        if persona not in updated:
                            updated.append(persona)
                        total_proposed += len(synthesized)
            except Exception:
                logger.exception("LLM synthesis failed for persona=%s", persona)

        # Legacy high-signal hardcoded rules (still valuable)
        result = train_persona(
            repo_key=FLEET_TIER,
            persona=persona,
            contribute_to_fleet=True,
            dry_run=dry_run,
        )

        if result.promoted or result.queued:
            updated.append(persona)
            total_proposed += len(result.queued) + len(result.promoted)
            total_promoted += len(result.promoted)

    return FleetSynthesisResult(
        personas_updated=updated,
        new_rules_proposed=total_proposed,
        promoted_to_fleet=total_promoted,
        details={"min_experience_rows": min_experience_rows},
    )


def trigger_fleet_learning_cycle(
    *,
    personas: list[str] | None = None,
    dry_run: bool = False,
) -> FleetSynthesisResult:
    """
    Entry point for the dispatcher / background loops to drive the flywheel.

    In practice, the best way to run synthesis is to dispatch the `fleet-learner`
    persona (using subagent-driven-development patterns) against ~/.agent-fleet/
    with rich context from get_synthesis_context().

    This thin wrapper still exists for the simple legacy path.
    """
    return synthesize_fleet_skills(
        personas=personas,
        dry_run=dry_run,
    )


def _run_llm_synthesis(
    *,
    backend: Any,  # noqa: ANN401
    persona: str,
    context: dict[str, str],
    timeout_s: int = 300,
) -> list[dict[str, Any]] | None:
    """Dispatch fleet-learner persona against accumulated experience.

    Returns parsed `skills` list, or None on failure / empty result.
    """
    if not _FLEET_LEARNER_PERSONA_PATH.is_file():
        logger.warning("fleet-learner.md not found at %s", _FLEET_LEARNER_PERSONA_PATH)
        return None
    persona_body = _FLEET_LEARNER_PERSONA_PATH.read_text(encoding="utf-8")

    prompt = (
        f"{persona_body}\n\n"
        f"## Target persona for this synthesis run\n\n`{persona}`\n\n"
        f"## Accumulated experience summary\n\n{context.get('summary', '(none)')}\n\n"
        f"## Recent samples\n\n{context.get('recent_samples', '(none)')}\n\n"
        "Now produce the JSON object described in the Strict Output Format section. "
        "Return ONLY the JSON — no prose, no markdown fence."
    )

    result = backend.run(
        prompt,
        max_tokens=0,
        timeout_s=timeout_s,
        cwd=LEVEL_UP_ROOT,
        allowed_tools=[],
    )
    if getattr(result, "exit_code", 0) != 0:
        logger.warning(
            "fleet-learner run failed: exit=%s stderr=%s",
            getattr(result, "exit_code", None),
            (getattr(result, "stderr", "") or "")[:300],
        )
        return None

    stdout = (getattr(result, "stdout", "") or "").strip()
    if not stdout:
        return None

    parsed = _extract_json(stdout)
    if not isinstance(parsed, dict):
        return None
    skills = parsed.get("skills")
    if not isinstance(skills, list):
        return None
    cleaned: list[dict[str, Any]] = []
    for s in skills:
        if not isinstance(s, dict):
            continue
        text = str(s.get("text", "")).strip()
        if not text:
            continue
        cleaned.append(
            {
                "kind": str(s.get("kind", "methodology")),
                "text": text,
                "evidence_summary": str(s.get("evidence_summary", "")).strip(),
                "confidence": float(s.get("confidence", 0.0) or 0.0),
            }
        )
    return cleaned or None


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json(text: str) -> Any:  # noqa: ANN401
    """Best-effort JSON extraction from a model response."""
    fence = _JSON_FENCE_RE.search(text)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass
    # Fallback: locate the first { and matching brace span.
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _persist_learning(
    *,
    persona: str,
    skills: list[dict[str, Any]],
    context: dict[str, str],
    dry_run: bool,
) -> bool:
    """Write synthesized skills to _fleet/<persona>/{learnings,skills_queue}."""
    if dry_run:
        return True
    fleet_persona = LEVEL_UP_ROOT / FLEET_TIER / persona
    learnings_dir = fleet_persona / "learnings"
    learnings_dir.mkdir(parents=True, exist_ok=True)

    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    md_lines = [
        f"# Fleet learning — {persona} — {ts}",
        "",
        f"Synthesized from {context.get('full_experience_count', '?')} recent experience rows.",
        "",
        "## Skills",
        "",
    ]
    for s in skills:
        md_lines.append(f"- **[{s['kind']}]** {s['text']}")
        if s["evidence_summary"]:
            md_lines.append(f"  - _evidence_: {s['evidence_summary']}")
        md_lines.append(f"  - _confidence_: {s['confidence']:.2f}")
    (learnings_dir / f"{ts}.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    queue_path = fleet_persona / "skills_queue.jsonl"
    with queue_path.open("a", encoding="utf-8") as fh:
        for s in skills:
            fh.write(json.dumps({"ts": ts, "persona": persona, **s}) + "\n")
    return True
