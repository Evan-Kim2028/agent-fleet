"""Structured logging and GitHub Actions step summary."""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

_run_start: float = 0.0
_step_summary_lines: list[str] = []

# Run-log state — populated by init_run_log()
_run_meta: dict = {}
_phase_starts: dict[str, float] = {}
_log_file: Path | None = None


def init(run_start: float) -> None:
    global _run_start, _step_summary_lines
    _run_start = run_start
    _step_summary_lines = []


def init_run_log(repo_root: Path, run_id: str, issue: int, persona: str) -> None:
    """Call once at dispatch startup to enable persistent NDJSON run logging."""
    global _run_meta, _log_file
    _run_meta = {"run_id": run_id, "issue": issue, "persona": persona}
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_dir = repo_root / "data" / "events" / "agent_runs"
    log_dir.mkdir(parents=True, exist_ok=True)
    _log_file = log_dir / f"{date_str}.ndjson"
    _append_event({"event": "run_start", "phase": None, "status": None, "duration_s": None, "detail": None})


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_event(fields: dict) -> None:
    if _log_file is None:
        return
    record = {"ts": _now_iso(), **_run_meta, **fields}
    try:
        with open(_log_file, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(tag: str, msg: str) -> None:
    elapsed = time.time() - _run_start if _run_start else 0
    print(f"[{_ts()}] [{tag}] (+{elapsed:.0f}s) {msg}", flush=True)


def append_step_summary(line: str) -> None:
    _step_summary_lines.append(line)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass


def init_step_summary(persona: str, issue_number: int, run_uuid: str) -> None:
    append_step_summary(f"# Agent Dispatch — {persona} / #{issue_number} / `{run_uuid}`\n")
    append_step_summary(f"| Persona | `{persona}` |")
    append_step_summary(f"| Issue | #{issue_number} |")
    append_step_summary(f"| Run ID | `{run_uuid}` |\n")
    append_step_summary("## Phase Log\n")


def record_phase(phase: str, status: str, detail: str = "") -> None:
    """Record a phase transition. Writes to GH step summary and NDJSON run log."""
    icon = {"started": "🔄", "complete": "✅", "skipped": "⏭️", "failed": "❌"}.get(status, "•")
    line = f"- {icon} **{phase}** — {status}"
    if detail:
        line += f": {detail}"
    append_step_summary(line)

    if status == "started":
        _phase_starts[phase] = time.monotonic()
        _append_event({"event": "phase_start", "phase": phase, "status": "started",
                       "duration_s": None, "detail": detail or None})
    else:
        start = _phase_starts.pop(phase, None)
        duration = round(time.monotonic() - start) if start is not None else None
        _append_event({"event": "phase_end", "phase": phase, "status": status,
                       "duration_s": duration, "detail": detail or None})


def record_run_end(status: str, detail: str = "", **extra: object) -> None:
    """Write the final run_end record with total elapsed time."""
    elapsed = round(time.time() - _run_start) if _run_start else None
    _append_event({"event": "run_end", "phase": "run", "status": status,
                   "duration_s": elapsed, "detail": detail or None, **extra})
