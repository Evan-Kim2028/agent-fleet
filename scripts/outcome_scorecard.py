#!/usr/bin/env python3
"""Aggregate task outcomes over agent-fleet run logs (JSONL).

Usage:
    python scripts/outcome_scorecard.py [--runs-dir DIR] [--json] [--include-synthetic] [--top N]
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from collections import Counter
from pathlib import Path

_ORCHESTRATION_PHASES = {
    "RESEARCH", "PLAN", "IMPLEMENT", "VERIFY", "SYNTHESIZE",
    "TECH_LEAD", "OPEN_PR",
}


def _default_runs_dir() -> Path:
    env = os.environ.get("AGENT_FLEET_RUNS_DIR")
    if env:
        return Path(env)
    return Path.home() / ".agent-fleet" / "fleet" / "runs"


def _is_real(events: list[dict]) -> bool:
    """A file is real if any llm.usage model != 'm', max tokens > 10000, or run.start present."""
    for e in events:
        if not isinstance(e, dict):
            continue
        if e.get("event") == "run.start":
            return True
        data = e.get("data")
        if not isinstance(data, dict):
            continue
        model = data.get("model")
        if isinstance(model, str) and model and model != "m":
            return True
        total_tok = data.get("total_tokens")
        if isinstance(total_tok, (int, float)) and total_tok > 10000:
            return True
    return False


def _infer_pipeline(phases: set[str]) -> str:
    lc = {p.lower() for p in phases}
    if "analyze" in lc:
        return "pr_review"
    orch = _ORCHESTRATION_PHASES & phases
    if orch - {"REVIEW"}:
        return "full"
    if "review" in lc and "execute" in lc:
        return "code_review"
    if "execute" in lc:
        return "simple"
    return "unknown"


def _most_common_model(events: list[dict]) -> str | None:
    counts: Counter[str] = Counter()
    for e in events:
        if not isinstance(e, dict):
            continue
        data = e.get("data")
        if not isinstance(data, dict):
            continue
        if e.get("event") == "llm.usage":
            model = data.get("model")
            if isinstance(model, str) and model:
                counts[model] += 1
    return counts.most_common(1)[0][0] if counts else None


def _parse_file(path: Path) -> dict | None:
    events: list[dict] = []
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                events.append(obj)
    except OSError:
        return None

    run_id: str | None = None
    goal: str | None = None
    persona: str | None = None
    last_complete: dict | None = None
    run_end: dict | None = None
    has_error = False
    tokens_total = 0
    fix_phase_tokens = 0
    phases: set[str] = set()
    rollup_tokens: int | None = None
    issue_top: int | None = None
    pr_number: int | None = None

    for e in events:
        if not isinstance(e, dict):
            continue

        ev = e.get("event")
        if not isinstance(ev, str):
            continue

        if run_id is None:
            cand = e.get("run_id")
            if isinstance(cand, str) and cand:
                run_id = cand

        if issue_top is None:
            it = e.get("issue_number")
            if isinstance(it, int):
                issue_top = it

        phase = e.get("phase")
        if isinstance(phase, str) and phase:
            phases.add(phase)

        if ev == "fleet.task.start":
            if goal is None:
                data = e.get("data")
                if isinstance(data, dict):
                    g = data.get("goal")
                    if isinstance(g, str):
                        goal = g
                    p = data.get("persona")
                    if isinstance(p, str) and persona is None:
                        persona = p

        elif ev == "fleet.task.complete":
            last_complete = e

        elif ev == "fleet.task.error":
            has_error = True

        elif ev == "llm.usage":
            data = e.get("data")
            if isinstance(data, dict):
                tok = data.get("total_tokens")
                if isinstance(tok, (int, float)):
                    tokens_total += int(tok)
                    ph = e.get("phase")
                    if isinstance(ph, str) and ph.lower() == "fix":
                        fix_phase_tokens += int(tok)

        elif ev == "llm.usage.task_rollup":
            data = e.get("data")
            if isinstance(data, dict):
                totals = data.get("totals", {})
                if isinstance(totals, dict):
                    t = totals.get("total_tokens")
                    if isinstance(t, (int, float)):
                        rollup_tokens = int(t)

        elif ev == "run.start":
            data = e.get("data")
            if isinstance(data, dict) and persona is None:
                p = e.get("persona") or data.get("persona")
                if isinstance(p, str):
                    persona = p

        elif ev == "run.end":
            run_end = e
            data = e.get("data")
            if isinstance(data, dict):
                prn = data.get("pr_number")
                if isinstance(prn, int):
                    pr_number = prn

    if persona is None:
        for e in events:
            if not isinstance(e, dict):
                continue
            p = e.get("persona")
            if isinstance(p, str) and p:
                persona = p
                break

    status: str
    fix_attempts = 0
    verify_attempts = 0
    verify_failure: bool | None = None
    changed_files_count: int | None = None
    issue_number: int | None = None
    repo_key: str | None = None
    duration_seconds: float | None = None

    # Prefer the orchestration/issue-loop verdict (run.end.outcome), then the dispatch
    # verdict (fleet.task.complete.status), then a bare error, then incomplete.
    resolved: str | None = None
    source_data: dict = {}
    if run_end is not None:
        d = run_end.get("data")
        if isinstance(d, dict):
            oc = d.get("outcome")
            if isinstance(oc, (str, int, float)) and str(oc):
                resolved = str(oc)
                source_data = d
    if resolved is None and last_complete is not None:
        d = last_complete.get("data")
        if isinstance(d, dict):
            source_data = d
            raw_status = d.get("status")
            if isinstance(raw_status, (str, int, float)):
                resolved = str(raw_status)
            dur = d.get("duration_seconds")
            if isinstance(dur, (int, float)):
                duration_seconds = float(dur)
    status = resolved if resolved is not None else ("error" if has_error else "incomplete")

    om = source_data.get("outcome_metrics")
    if isinstance(om, dict):
        fa = om.get("fix_attempts")
        if isinstance(fa, int):
            fix_attempts = fa
        va = om.get("verify_attempts")
        if isinstance(va, int):
            verify_attempts = va
        vf = om.get("verify_failure")
        if vf is not None:
            verify_failure = bool(vf)
        cfc = om.get("changed_files_count")
        if isinstance(cfc, int):
            changed_files_count = cfc
        iss = om.get("issue_number")
        if isinstance(iss, int):
            issue_number = iss
        rk = om.get("repo_key")
        if isinstance(rk, str) and rk:
            repo_key = rk
    if issue_number is None:
        issue_number = issue_top

    if tokens_total == 0 and rollup_tokens is not None:
        tokens_total = rollup_tokens

    return {
        "run_id": run_id if run_id is not None else path.stem,
        "file": path.name,
        "kind": "real" if _is_real(events) else "synthetic",
        "goal": goal,
        "persona": persona,
        "model": _most_common_model(events),
        "pipeline": _infer_pipeline(phases),
        "status": status,
        "success": status == "completed",
        "fix_attempts": fix_attempts,
        "verify_attempts": verify_attempts,
        "verify_failure": verify_failure,
        "changed_files_count": changed_files_count,
        "issue_number": issue_number,
        "pr_number": pr_number,
        "repo_key": repo_key,
        "tokens_total": tokens_total,
        "fix_phase_tokens": fix_phase_tokens,
        "duration_seconds": duration_seconds,
    }


def _load_all(runs_dir: Path) -> list[dict]:
    rows = []
    for jsonl in sorted(runs_dir.glob("*.jsonl")):
        row = _parse_file(jsonl)
        if row is not None:
            rows.append(row)
    return rows


def _build_summary(rows: list[dict]) -> dict:
    if not rows:
        return {
            "total": 0,
            "success_rate": None,
            "status_histogram": {},
            "tokens_by_status": {},
            "spiral": {},
            "by_persona": {},
            "by_pipeline": {},
            "by_repo_key": {},
            "by_model": {},
        }

    total = len(rows)
    success_count = sum(1 for r in rows if r["success"])
    success_rate = round(success_count / total, 4) if total else None

    status_counts: Counter[str] = Counter(r["status"] for r in rows)
    status_histogram = {
        s: {"count": c, "pct": round(c / total, 4)}
        for s, c in status_counts.most_common()
    }

    tokens_by_status: dict[str, int] = {}
    for r in rows:
        st = r["status"]
        tokens_by_status[st] = tokens_by_status.get(st, 0) + r["tokens_total"]

    total_tok = sum(r["tokens_total"] for r in rows) or 1
    fix_tok = sum(r["fix_phase_tokens"] for r in rows)
    spiral_runs = [r for r in rows if r["fix_attempts"] > 0 or r["verify_attempts"] > 1]

    spiral = {
        "count": len(spiral_runs),
        "fix_phase_token_share": round(fix_tok / total_tok, 4),
        "total_fix_phase_tokens": fix_tok,
        "top_by_fix_phase_tokens": sorted(
            [
                {
                    "run_id": r["run_id"],
                    "fix_phase_tokens": r["fix_phase_tokens"],
                    "status": r["status"],
                    "repo_key": r["repo_key"],
                }
                for r in rows
            ],
            key=lambda x: x["fix_phase_tokens"],
            reverse=True,
        )[:5],
    }

    def _breakdown(key: str) -> dict:
        groups: dict[str, list[dict]] = {}
        for r in rows:
            k = str(r.get(key) or "unknown")
            groups.setdefault(k, []).append(r)
        out = {}
        for k, group in sorted(groups.items()):
            sc = sum(1 for g in group if g["success"])
            out[k] = {
                "count": len(group),
                "success_count": sc,
                "success_rate": round(sc / len(group), 4),
            }
        return out

    return {
        "total": total,
        "success_count": success_count,
        "success_rate": success_rate,
        "status_histogram": status_histogram,
        "tokens_by_status": tokens_by_status,
        "spiral": spiral,
        "by_persona": _breakdown("persona"),
        "by_pipeline": _breakdown("pipeline"),
        "by_repo_key": _breakdown("repo_key"),
        "by_model": _breakdown("model"),
    }


def _print_human(rows: list[dict], top_n: int, runs_dir: Path) -> None:
    real = [r for r in rows if r["kind"] == "real"]
    synth = [r for r in rows if r["kind"] == "synthetic"]
    total_scanned = len(rows)

    print(
        f"Scanned {total_scanned} files: {len(real)} real, {len(synth)} synthetic "
        f"(excluded). Real = model != 'm' OR max_tokens > 10000 OR run.start present."
    )

    if not real:
        print("No real runs found.")
        return

    s = _build_summary(real)
    total = s["total"]
    sc = s.get("success_count", 0)

    print(f"\nSuccess rate: {sc}/{total} ({100 * (s['success_rate'] or 0):.1f}%)\n")

    print("Status histogram:")
    for status, info in s["status_histogram"].items():
        bar = "#" * int(info["pct"] * 40)
        print(f"  {status:<30}  {info['count']:>5}  {100*info['pct']:>5.1f}%  {bar}")

    print("\nTokens by status:")
    for st, tok in sorted(s["tokens_by_status"].items(), key=lambda x: -x[1]):
        print(f"  {st:<30}  {tok:>12,}")

    sp = s["spiral"]
    fix_share_pct = 100 * sp["fix_phase_token_share"]
    print(
        f"\nSpiral signal: {sp['count']} runs with fix_attempts>0 or verify_attempts>1, "
        f"fix-phase token share {fix_share_pct:.1f}%"
    )
    if sp["top_by_fix_phase_tokens"]:
        print("  Top runs by fix_phase_tokens:")
        for r in sp["top_by_fix_phase_tokens"]:
            print(
                f"    {r['run_id']:<36}  fix_tok={r['fix_phase_tokens']:>8,}"
                f"  status={r['status']}  repo={r['repo_key']}"
            )

    for label, key in [
        ("By persona", "by_persona"),
        ("By pipeline", "by_pipeline"),
        ("By repo_key", "by_repo_key"),
        ("By model", "by_model"),
    ]:
        breakdown = s[key]
        print(f"\n{label}:")
        for k, info in breakdown.items():
            print(
                f"  {k:<30}  count={info['count']:>5}  "
                f"success={info['success_count']:>5}  "
                f"rate={100*info['success_rate']:>5.1f}%"
            )

    failures = sorted(
        [r for r in real if not r["success"]],
        key=lambda r: r["tokens_total"],
        reverse=True,
    )[:top_n]
    print(f"\nTop {top_n} highest-token non-completed runs (capability killers):")
    hdr = f"  {'run_id':<36}  {'status':<25}  {'tokens':>10}  {'repo':<18}  goal"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for r in failures:
        goal_trunc = (r["goal"] or "")[:50]
        print(
            f"  {r['run_id']:<36}  {r['status']:<25}  {r['tokens_total']:>10,}"
            f"  {r['repo_key'] or ''!s:<18}  {goal_trunc}"
        )

    print(
        "\nNote: no version is stamped in the logs; monthly buckets below use file mtime"
        " as a proxy, not a per-version measurement."
    )
    buckets: dict[str, list[bool]] = {}
    for r in real:
        p = runs_dir / r["file"]
        try:
            mtime = p.stat().st_mtime
            month = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m")
        except OSError:
            month = "unknown"
        buckets.setdefault(month, []).append(r["success"])

    print("\nMonthly success-rate proxy (file mtime):")
    for month in sorted(buckets):
        hits = buckets[month]
        rate = sum(hits) / len(hits)
        print(
            f"  {month}  count={len(hits):>4}  "
            f"success={sum(hits):>4}  rate={100*rate:>5.1f}%"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Aggregate task outcomes from agent-fleet run logs."
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help=(
            "Directory containing *.jsonl run logs"
            " (default: AGENT_FLEET_RUNS_DIR or ~/.agent-fleet/fleet/runs)"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="output_json",
        help="Emit a JSON object with 'runs' and 'summary' keys.",
    )
    parser.add_argument(
        "--include-synthetic",
        action="store_true",
        help="Include synthetic fixtures in the summary (default: real only).",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        metavar="N",
        help="Number of top failures to show (default: 10).",
    )
    args = parser.parse_args(argv)

    runs_dir: Path = args.runs_dir or _default_runs_dir()
    if not runs_dir.is_dir():
        print(f"runs-dir not found: {runs_dir}", file=sys.stderr)
        return 1

    all_rows = _load_all(runs_dir)

    summary_rows = (
        all_rows if args.include_synthetic else [r for r in all_rows if r["kind"] == "real"]
    )

    if args.output_json:
        print(
            json.dumps(
                {"runs": all_rows, "summary": _build_summary(summary_rows)},
                indent=2,
            )
        )
    else:
        _print_human(all_rows, top_n=args.top, runs_dir=runs_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
