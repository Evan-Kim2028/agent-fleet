"""CLI for viewing agent run history from NDJSON event logs."""

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path


def _find_repo_root() -> Path | None:
    """Walk up from cwd looking for a .git directory."""
    p = Path.cwd()
    for _ in range(10):
        if (p / ".git").exists():
            return p
        p = p.parent
    return None


def _log_dir(repo_root: Path) -> Path:
    return repo_root / "data" / "events" / "agent_runs"


def _load_records(log_dir: Path, days: int = 7) -> list[dict]:
    records: list[dict] = []
    today = date.today()
    for offset in range(days):
        d = today - timedelta(days=offset)
        f = log_dir / f"{d.isoformat()}.ndjson"
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


def _group_runs(records: list[dict]) -> dict[str, list[dict]]:
    runs: dict[str, list[dict]] = {}
    for r in records:
        run_id = r.get("run_id")
        if run_id:
            runs.setdefault(run_id, []).append(r)
    return runs


def _summarise_run(run_id: str, events: list[dict]) -> dict:
    events = sorted(events, key=lambda e: e.get("ts", ""))
    first = events[0]
    summary = {
        "run_id": run_id,
        "issue": first.get("issue"),
        "persona": first.get("persona"),
        "started": first.get("ts"),
        "status": None,
        "elapsed_s": None,
        "phases": [],
    }
    for e in events:
        if e.get("event") == "run_end":
            summary["status"] = e.get("status")
            summary["elapsed_s"] = e.get("duration_s")
        if e.get("event") == "phase_end":
            summary["phases"].append({
                "phase": e.get("phase"),
                "status": e.get("status"),
                "duration_s": e.get("duration_s"),
                "detail": e.get("detail"),
            })
    return summary


def _print_run(summary: dict, show_phases: bool) -> None:
    status_icon = {"complete": "✅", "failed": "❌", "skipped": "⏭️"}.get(
        summary["status"] or "", "🔄"
    )
    elapsed = f"{summary['elapsed_s']}s" if summary["elapsed_s"] is not None else "—"
    print(
        f"{status_icon} run={summary['run_id']}  issue=#{summary['issue']}  "
        f"persona={summary['persona']}  started={summary['started']}  elapsed={elapsed}"
    )
    if show_phases:
        for p in summary["phases"]:
            icon = {"complete": "  ✅", "failed": "  ❌", "skipped": "  ⏭️"}.get(
                p.get("status") or "", "  •"
            )
            dur = f"{p['duration_s']}s" if p.get("duration_s") is not None else "—"
            detail = f"  [{p['detail']}]" if p.get("detail") else ""
            print(f"    {icon} {p['phase']:<20} {dur:>6}{detail}")


def main() -> None:
    parser = argparse.ArgumentParser(description="View agent run history.")
    parser.add_argument("--last", type=int, default=10, metavar="N",
                        help="Show last N runs (default: 10)")
    parser.add_argument("--issue", type=int, default=None, metavar="N",
                        help="Filter by issue number")
    parser.add_argument("--phases", action="store_true",
                        help="Show per-phase breakdown")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="Output raw JSON")
    parser.add_argument("--days", type=int, default=7,
                        help="How many days of logs to scan (default: 7)")
    args = parser.parse_args()

    repo_root = _find_repo_root()
    if repo_root is None:
        print("ERROR: not inside a git repository.", file=sys.stderr)
        sys.exit(1)

    log_dir = _log_dir(repo_root)
    if not log_dir.exists():
        print("No agent run logs found (no data/events/agent_runs/ directory).")
        return

    records = _load_records(log_dir, days=args.days)
    if not records:
        print("No agent run records found.")
        return

    runs = _group_runs(records)
    summaries = [_summarise_run(rid, evts) for rid, evts in runs.items()]
    summaries.sort(key=lambda s: s.get("started") or "", reverse=True)

    if args.issue is not None:
        summaries = [s for s in summaries if s.get("issue") == args.issue]

    summaries = summaries[: args.last]

    if args.as_json:
        print(json.dumps(summaries, indent=2))
        return

    if not summaries:
        print("No matching runs.")
        return

    for s in summaries:
        _print_run(s, show_phases=args.phases)
