"""Run multi-pass PR analysis via fleet LLM backends."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_fleet.pr_review.config import PrReviewConfig
from agent_fleet.pr_review.git import classify_files, is_deletion_only_pr
from agent_fleet.pr_review.prompts import build_prompt

if TYPE_CHECKING:
    from agent_fleet.hooks import LLMBackend

_DELETION_INAPPLICABLE = {"integration-tests-present", "debug-code-removed"}


def _extract_json(text: str) -> dict[str, Any]:
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object found in LLM output")
    depth = 0
    for index, char in enumerate(text[start:], start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : index + 1]
                cleaned = re.sub(r"^```json\s*", "", candidate.strip())
                cleaned = re.sub(r"\s*```$", "", cleaned).strip()
                return json.loads(cleaned)
    raise ValueError("unterminated JSON object in LLM output")


def _log_analysis(
    config: PrReviewConfig,
    *,
    pr_number: int,
    mode: str,
    prompt: str,
    raw_output: str,
    parsed: dict[str, Any] | None,
    error: str | None = None,
) -> None:
    log_dir = config.log_dir or Path.home() / ".agent-fleet" / "pr-review-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    payload = {
        "meta": {
            "pr_number": pr_number,
            "mode": mode,
            "timestamp_utc": timestamp,
            "prompt_chars": len(prompt),
            "output_chars": len(raw_output),
            "error": error,
        },
        "prompt": prompt,
        "raw_output": raw_output,
        "parsed": parsed,
    }
    path = log_dir / f"pr{pr_number}_{mode}_{timestamp}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _higher_risk(left: str, right: str) -> str:
    order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    return left if order.get(left.lower(), 0) >= order.get(right.lower(), 0) else right


def merge_analyses(analyses: list[dict[str, Any]]) -> dict[str, Any]:
    if not analyses:
        return {
            "pr_type": "other",
            "primary_areas": [],
            "risk_level": "low",
            "risk_reasoning": "No analysis performed.",
            "summary": "No analysis performed.",
            "deep_analysis": "",
            "recommendations": {},
            "findings": [],
            "suggestions": [],
        }
    if len(analyses) == 1:
        return analyses[0]

    merged: dict[str, Any] = {
        "pr_type": "mixed",
        "primary_areas": [],
        "risk_level": "low",
        "risk_reasoning": "",
        "summary": "",
        "deep_analysis": "",
        "recommendations": {},
        "findings": [],
        "suggestions": [],
    }
    all_areas: set[str] = set()
    rec_keys = {
        "frontend_check",
        "backend_check",
        "pipeline_check",
        "security_check",
        "qa_check",
        "performance_check",
        "data_check",
        "ops_check",
    }
    for analysis in analyses:
        merged["risk_level"] = _higher_risk(
            str(merged["risk_level"]),
            str(analysis.get("risk_level", "low")),
        )
        all_areas.update(analysis.get("primary_areas", []))
        for key in ("risk_reasoning", "summary", "deep_analysis"):
            value = str(analysis.get(key) or "").strip()
            if value:
                merged[key] = f"{merged[key]}\n\n{value}".strip()
        recs = analysis.get("recommendations") or {}
        for key in rec_keys:
            merged["recommendations"][key] = merged["recommendations"].get(key, False) or recs.get(
                key, False
            )
        merged["findings"].extend(analysis.get("findings") or [])
        merged["suggestions"].extend(analysis.get("suggestions") or [])
        checklist = analysis.get("methodology_checklist")
        if checklist:
            existing = merged.get("methodology_checklist") or {}
            for ck, cv in checklist.items():
                if ck.endswith("_present") or ck.endswith("_verified"):
                    existing[ck] = existing.get(ck, False) or bool(cv)
                elif cv:
                    existing[ck] = cv
            merged["methodology_checklist"] = existing

    merged["primary_areas"] = sorted(all_areas)
    seen: set[str] = set()
    deduped: list[str] = []
    for suggestion in merged["suggestions"]:
        key = str(suggestion).lower().strip()
        if key and key not in seen:
            seen.add(key)
            deduped.append(str(suggestion))
    merged["suggestions"] = deduped
    return merged


def cap_findings_for_deletion_only(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    capped: list[dict[str, Any]] = []
    for finding in findings:
        if finding.get("methodology") in _DELETION_INAPPLICABLE:
            continue
        item = dict(finding)
        item["severity"] = "low"
        capped.append(item)
    return capped


def _run_pass(
    *,
    diff: str,
    files: list[str],
    mode: str,
    config: PrReviewConfig,
    backend: LLMBackend,
    workspace: Path,
    pr_number: int,
    model: str | None,
    timeout_s: int,
    allowed_tools: list[str] | None,
    skill_dirs: list | None = None,
) -> dict[str, Any] | None:
    prompt = build_prompt(diff, files, mode, config, skill_dirs=skill_dirs or [])
    result = backend.run(
        prompt,
        max_tokens=8192,
        timeout_s=timeout_s,
        cwd=workspace,
        model=model,
        mode="plan",
        allowed_tools=allowed_tools or ["Read", "Grep"],
    )
    if result.exit_code != 0:
        _log_analysis(
            config,
            pr_number=pr_number,
            mode=f"{mode}_error",
            prompt=prompt,
            raw_output=result.stdout,
            parsed=None,
            error=result.stderr or f"exit {result.exit_code}",
        )
        return None
    try:
        parsed = _extract_json(result.stdout)
    except (ValueError, json.JSONDecodeError) as exc:
        _log_analysis(
            config,
            pr_number=pr_number,
            mode=f"{mode}_parse_error",
            prompt=prompt,
            raw_output=result.stdout,
            parsed=None,
            error=str(exc),
        )
        return None
    _log_analysis(
        config,
        pr_number=pr_number,
        mode=mode,
        prompt=prompt,
        raw_output=result.stdout,
        parsed=parsed,
    )
    return parsed


def passes_for_files(files: list[str], config: PrReviewConfig) -> list[str]:
    classified = classify_files(files, config.area_prefixes)
    selected: list[str] = []
    if "backend-security" in config.passes:
        selected.append("backend-security")
    if "frontend" in config.passes and classified["frontend"]:
        selected.append("frontend")
    if config.quality_review_enabled and "quality" not in selected:
        selected.append("quality")
    return selected or ["backend-security"]


def analyze_changes(
    *,
    diff: str,
    files: list[str],
    config: PrReviewConfig,
    backend: LLMBackend,
    workspace: Path,
    pr_number: int = 0,
    model: str | None = None,
    timeout_s: int = 900,
    allowed_tools: list[str] | None = None,
    skill_dirs: list | None = None,
) -> dict[str, Any]:
    """Run configured analysis passes and return merged JSON analysis."""
    modes = passes_for_files(files, config)
    analyses: list[dict[str, Any]] = []
    for mode in modes:
        parsed = _run_pass(
            diff=diff,
            files=files,
            mode=mode,
            config=config,
            backend=backend,
            workspace=workspace,
            pr_number=pr_number,
            model=model,
            timeout_s=timeout_s,
            allowed_tools=allowed_tools,
            skill_dirs=skill_dirs,
        )
        if parsed:
            analyses.append(parsed)

    merged = merge_analyses(analyses)
    if is_deletion_only_pr(diff):
        merged["findings"] = cap_findings_for_deletion_only(merged.get("findings") or [])
        if merged.get("risk_level") in {"high", "critical"}:
            merged["risk_level"] = "medium"
    return merged
