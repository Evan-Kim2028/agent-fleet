"""Per-run outcome metrics for fleet logs and repo-scoped level-up experience."""

from __future__ import annotations

from typing import Any

_SNIPPET_MAX = 500
_VERIFY_LOOP_ALERT = 2
_FIX_TOKEN_RATIO_ALERT = 0.5


def _snippet(text: str | None, *, max_len: int = _SNIPPET_MAX) -> str:
    if not text:
        return ""
    cleaned = str(text).strip()
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 3] + "..."


def _iter_phase_entries(
    phases: list[dict[str, Any]] | dict[str, Any] | None,
) -> list[tuple[str, dict[str, Any]]]:
    if not phases:
        return []
    if isinstance(phases, list):
        out: list[tuple[str, dict[str, Any]]] = []
        for item in phases:
            if not isinstance(item, dict):
                continue
            name = str(item.get("phase") or "unknown")
            out.append((name, item))
        return out
    if isinstance(phases, dict):
        entries: list[tuple[str, dict[str, Any]]] = []
        for key, value in phases.items():
            if not isinstance(value, dict):
                continue
            name = str(value.get("phase") or key)
            entries.append((str(key), value))
        return entries
    return []


def _failed_check(checks: list[dict[str, Any]]) -> dict[str, Any] | None:
    for check in checks:
        if not isinstance(check, dict):
            continue
        if check.get("passed") is False:
            return check
    return None


def extract_verify_failure(
    phases: list[dict[str, Any]] | dict[str, Any] | None,
    *,
    error: str | None = None,
) -> dict[str, Any] | None:
    """Return structured verify/bootstrap failure details when present."""
    bootstrap: dict[str, Any] | None = None
    verify: dict[str, Any] | None = None

    for phase_key, item in _iter_phase_entries(phases):
        checks = item.get("checks")
        if isinstance(checks, list):
            failed = _failed_check(checks)
            if failed is None:
                continue
            name = str(failed.get("name") or "")
            record = {
                "phase": phase_key,
                "command": name.removeprefix("bootstrap: ").strip() or name,
                "exit_code": failed.get("exit_code"),
                "stderr_snippet": _snippet(
                    str(failed.get("stderr_tail") or failed.get("detail") or "")
                ),
                "stdout_snippet": _snippet(str(failed.get("stdout_tail") or "")),
            }
            if name.startswith("bootstrap:"):
                bootstrap = {**record, "kind": "bootstrap"}
            elif verify is None:
                verify = {**record, "kind": "verify"}

        if item.get("phase") == "verify" and not item.get("passed", True):
            verify = {
                "phase": "verify",
                "kind": "verify",
                "command": str(item.get("command") or "verify"),
                "exit_code": item.get("exit_code"),
                "stderr_snippet": _snippet(str(item.get("stderr") or "")),
                "stdout_snippet": _snippet(str(item.get("stdout") or "")),
            }

        severity = str(item.get("severity") or "")
        if severity and severity not in {"ok", "OK"}:
            message = str(item.get("message") or "")
            if "bootstrap" in message.lower() and bootstrap is None:
                bootstrap = {
                    "phase": phase_key,
                    "kind": "bootstrap",
                    "command": message.split(":", 1)[-1].strip()[:200],
                    "exit_code": None,
                    "stderr_snippet": _snippet(message),
                    "stdout_snippet": "",
                }
            elif verify is None and (
                phase_key.startswith("VERIFY") or item.get("phase") == "verify"
            ):
                verify = {
                    "phase": phase_key,
                    "kind": "verify",
                    "command": message.split(":", 1)[-1].strip()[:200] or phase_key,
                    "exit_code": None,
                    "stderr_snippet": _snippet(message),
                    "stdout_snippet": "",
                }

    if bootstrap is not None:
        return bootstrap
    if verify is not None:
        return verify
    if error and "verify" in error.lower():
        return {
            "phase": None,
            "kind": "verify",
            "command": None,
            "exit_code": None,
            "stderr_snippet": _snippet(error),
            "stdout_snippet": "",
        }
    return None


def count_verify_fix_loops(
    phases: list[dict[str, Any]] | dict[str, Any] | None,
) -> tuple[int, int]:
    """Return (verify_attempts, fix_attempts) inferred from phase artifacts."""
    if isinstance(phases, dict):
        verify_keys = [k for k in phases if str(k).startswith("VERIFY")]
        verify_attempts = len(verify_keys) if verify_keys else 0
        if verify_attempts == 0:
            for _key, value in phases.items():
                if isinstance(value, dict) and value.get("phase") == "verify":
                    verify_attempts += 1
        fix_attempts = max(0, verify_attempts - 1)
        return verify_attempts, fix_attempts

    verify_attempts = 0
    for _name, item in _iter_phase_entries(phases):
        if item.get("phase") == "verify":
            verify_attempts += 1
    fix_attempts = max(0, verify_attempts - 1)
    return verify_attempts, fix_attempts


def extract_complexity_ceiling(
    phases: list[dict[str, Any]] | dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return complexity ceiling breach recorded as a phase metric, if any."""
    for _name, item in _iter_phase_entries(phases):
        if item.get("phase") == "complexity" and item.get("metric_only"):
            return {
                k: item[k]
                for k in (
                    "declared_complexity",
                    "observed_total_tokens",
                    "ceiling",
                    "over_by",
                    "efficiency_ratio",
                )
                if k in item
            }
    return None


def phase_token_counts(
    usage_rollup: dict[str, Any] | None,
) -> tuple[int, int]:
    """Return (total_tokens, fix_token_total) from a usage_rollup snapshot.

    Falls back to summing by_phase buckets when the top-level totals key is
    absent or zero, matching the rollup shape produced by RunLog.
    """
    if not usage_rollup:
        return 0, 0
    by_phase = usage_rollup.get("by_phase") or {}
    totals = usage_rollup.get("totals")
    total_tokens = 0
    if isinstance(totals, dict):
        total_tokens = int(totals.get("total_tokens") or 0)
    if total_tokens <= 0:
        for bucket in by_phase.values():
            if isinstance(bucket, dict):
                total_tokens += int(bucket.get("total_tokens") or 0)
    fix_token_total = 0
    for phase_name, bucket in by_phase.items():
        if isinstance(bucket, dict) and str(phase_name).upper().startswith("FIX"):
            fix_token_total += int(bucket.get("total_tokens") or 0)
    return total_tokens, fix_token_total


def fix_phase_ratio(usage_rollup: dict[str, Any] | None) -> float:
    """Return the fraction of total tokens consumed by FIX-prefixed phases.

    Returns 0.0 when there is no usage data or the total is zero.
    """
    if not usage_rollup:
        return 0.0

    by_phase = usage_rollup.get("by_phase")
    if not isinstance(by_phase, dict):
        return 0.0

    total_tokens, fix_tokens = phase_token_counts(usage_rollup)
    if total_tokens <= 0:
        return 0.0
    return fix_tokens / total_tokens


def build_cost_alerts(
    *,
    usage_rollup: dict[str, Any] | None,
    verify_attempts: int,
) -> list[str]:
    """Flag expensive verify/fix patterns for per-repo tuning."""
    alerts: list[str] = []
    if verify_attempts > _VERIFY_LOOP_ALERT:
        alerts.append("verify_retries_high")

    if not usage_rollup:
        return alerts

    if fix_phase_ratio(usage_rollup) > _FIX_TOKEN_RATIO_ALERT:
        alerts.append("fix_phase_token_ratio_high")
    return alerts


def build_run_metrics(
    *,
    status: str,
    phases: list[dict[str, Any]] | dict[str, Any] | None = None,
    error: str | None = None,
    pr_number: int | None = None,
    pr_loop_status: str | None = None,
    review_verdict: str | None = None,
    usage_rollup: dict[str, Any] | None = None,
    changed_files_count: int | None = None,
    duration_seconds: float | None = None,
    repo_key: str | None = None,
    issue_number: int | None = None,
) -> dict[str, Any]:
    """Structured rollup attached to fleet.task.complete, run.end, and experience rows."""
    verify_attempts, fix_attempts = count_verify_fix_loops(phases)
    verify_failure = extract_verify_failure(phases, error=error)
    bootstrap_failure = (
        verify_failure if verify_failure and verify_failure.get("kind") == "bootstrap" else None
    )
    if verify_failure and verify_failure.get("kind") == "bootstrap":
        verify_failure = None

    metrics: dict[str, Any] = {
        "status": status,
        "verify_attempts": verify_attempts,
        "fix_attempts": fix_attempts,
    }
    if repo_key:
        metrics["repo_key"] = repo_key
    if issue_number is not None:
        metrics["issue_number"] = issue_number
    if duration_seconds is not None:
        metrics["duration_seconds"] = duration_seconds
    if changed_files_count is not None:
        metrics["changed_files_count"] = changed_files_count
    if pr_number is not None:
        metrics["pr_number"] = pr_number
    if pr_loop_status:
        metrics["pr_loop_status"] = pr_loop_status
    if review_verdict:
        metrics["review_verdict"] = review_verdict
    if verify_failure:
        metrics["verify_failure"] = verify_failure
    if bootstrap_failure:
        metrics["bootstrap_failure"] = bootstrap_failure
    if usage_rollup:
        metrics["token_rollup"] = {
            "calls": usage_rollup.get("calls"),
            "duration_s": usage_rollup.get("duration_s"),
            "totals": usage_rollup.get("totals"),
            "by_phase": usage_rollup.get("by_phase"),
        }

    complexity_ceiling = extract_complexity_ceiling(phases)
    if complexity_ceiling:
        metrics["complexity_ceiling"] = complexity_ceiling

    alerts = build_cost_alerts(
        usage_rollup=usage_rollup,
        verify_attempts=verify_attempts,
    )
    if complexity_ceiling:
        alerts = list(alerts) if alerts else []
        if "complexity_ceiling_exceeded" not in alerts:
            alerts.append("complexity_ceiling_exceeded")
    if alerts:
        metrics["cost_alerts"] = alerts
    return metrics
