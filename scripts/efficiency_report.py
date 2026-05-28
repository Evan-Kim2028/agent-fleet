#!/usr/bin/env python3
"""Summarize per-run token efficiency from agent-fleet run logs (JSONL).

Usage:
    python scripts/efficiency_report.py [--runs-dir DIR] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_EVENT = "llm.usage.task_rollup"


def _default_runs_dir() -> Path:
    env = os.environ.get("AGENT_FLEET_RUNS_DIR")
    if env:
        return Path(env)
    return Path.home() / ".agent-fleet" / "fleet" / "runs"


def _parse_file(path: Path) -> dict | None:
    """Return the most recent task_rollup event data from a JSONL file, or None."""
    last = None
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if obj.get("event") == _EVENT:
                last = obj
    except OSError:
        return None
    if last is None:
        return None
    return last.get("data")


def _summarize(runs_dir: Path) -> list[dict]:
    rows = []
    for jsonl in sorted(runs_dir.glob("*.jsonl")):
        data = _parse_file(jsonl)
        if data is None:
            continue
        run_id = jsonl.stem
        totals = data.get("totals", {})
        total_tokens: int = totals.get("total_tokens") or sum(
            v for v in totals.values() if isinstance(v, int)
        )
        changed = data.get("changed_lines", 0) or 0
        tpl = data.get("tokens_per_changed_line")
        if tpl is None:
            tpl = round(total_tokens / max(changed, 1))
        by_phase = data.get("by_phase", {})
        phase_breakdown = {
            phase: info.get("total_tokens", 0) for phase, info in by_phase.items()
        }
        rows.append(
            {
                "run_id": run_id,
                "total_tokens": total_tokens,
                "changed_lines": changed,
                "tokens_per_changed_line": tpl,
                "by_phase": phase_breakdown,
            }
        )
    rows.sort(key=lambda r: r["total_tokens"], reverse=True)
    return rows


def _print_table(rows: list[dict]) -> None:
    if not rows:
        print("No llm.usage.task_rollup events found.")
        return
    header = (
        f"{'run_id':<36}  {'total_tokens':>12}  {'changed_lines':>13}"
        f"  {'tok/changed_ln':>14}  phases"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        phases = "  ".join(f"{p}:{t}" for p, t in r["by_phase"].items())
        print(
            f"{r['run_id']:<36}  {r['total_tokens']:>12}  {r['changed_lines']:>13}"
            f"  {r['tokens_per_changed_line']:>14}  {phases}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize agent-fleet run token efficiency.")
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
        help="Emit a JSON array instead of the text table.",
    )
    args = parser.parse_args(argv)

    runs_dir: Path = args.runs_dir or _default_runs_dir()
    if not runs_dir.is_dir():
        print(f"runs-dir not found: {runs_dir}", file=sys.stderr)
        return 1

    rows = _summarize(runs_dir)
    if args.output_json:
        print(json.dumps(rows, indent=2))
    else:
        _print_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
