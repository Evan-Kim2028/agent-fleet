"""fleet-status — one-shot summary of in-flight fleet runs, locks, recent PRs.

Designed for other runtimes (e.g. takopi-runtime claude sessions) to read the
fleet's state via the same gh CLI everything else uses. Primary signal is
GitHub (``agent-running/*`` labels); optional enrichment comes from
``agent-fleet-watch`` issue state (``.agent-fleet-issue-state.json``).
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_REPO = "Evan-Kim2028/silphcoanalytics"

# Written by agent-fleet-watch issue_loop (see agent_fleet.issue_loop.config).
_FLEET_ISSUE_STATE_NAME = ".agent-fleet-issue-state.json"


def _find_repo_root(start: Path | None = None) -> Path:
    """Walk up from *start* (default cwd) looking for .agent-fleet.yaml or .git."""
    cur = (start or Path.cwd()).resolve()
    for directory in (cur, *cur.parents):
        if (directory / ".agent-fleet.yaml").exists() or (directory / ".git").exists():
            return directory
    return cur


def _fleet_issue_state_path() -> Path:
    return _find_repo_root() / _FLEET_ISSUE_STATE_NAME


def _gh(*args: str) -> str:
    env = {**os.environ}
    env.pop("GITHUB_TOKEN", None)
    env.pop("GH_TOKEN", None)
    result = subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=True, env=env
    )
    return result.stdout


def _fleet_in_flight_from_state() -> dict[str, list[dict]]:
    """Read agent-fleet issue state. Returns {} on any error."""
    try:
        data = json.loads(_fleet_issue_state_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return {}
    raw = data.get("in_flight", {}) or {}
    out: dict[str, list[dict]] = {}
    for issue_key, entry in raw.items():
        if isinstance(entry, list):
            out[str(issue_key)] = entry
        elif isinstance(entry, dict):
            out[str(issue_key)] = [entry]
    return out


def _pid_alive(pid: int) -> bool:
    """True if /proc/<pid> still exists and points at an agents.dispatch process."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"agents.dispatch" in f.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return False


def _agent_running_issues(repo: str) -> list[dict]:
    out = _gh(
        "issue", "list", "--repo", repo, "--state", "open",
        "--search", "label:agent-running",
        "--json", "number,title,labels", "--limit", "50",
    )
    issues = json.loads(out)
    rows: list[dict] = []
    for issue in issues:
        locks = [
            label["name"] for label in issue.get("labels", [])
            if label["name"].startswith("agent-running")
        ]
        rows.append({"number": issue["number"], "title": issue["title"], "locks": locks})
    return rows


def _fleet_prs(repo: str, limit: int = 20) -> list[dict]:
    out = _gh(
        "pr", "list", "--repo", repo, "--state", "all",
        "--search", "head:fleet/",
        "--json", "number,title,state,isDraft,headRefName,createdAt",
        "--limit", str(limit),
    )
    return json.loads(out)


def _last_agent_comment(repo: str, issue_number: int) -> str | None:
    out = _gh(
        "issue", "view", str(issue_number), "--repo", repo,
        "--json", "comments",
    )
    data = json.loads(out)
    pattern = re.compile(r"^(?:🤖|Agent (?:opened|rejected|escalated|deferred))", re.M)
    for c in reversed(data.get("comments", [])):
        if pattern.search(c.get("body", "")):
            return f"[{c['createdAt']}] {c['body'].splitlines()[0][:140]}"
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Snapshot agent fleet state from GitHub")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="owner/name (default %(default)s)")
    parser.add_argument(
        "--json", action="store_true",
        help="Emit a machine-readable JSON document instead of the human summary.",
    )
    args = parser.parse_args()

    try:
        locks = _agent_running_issues(args.repo)
        prs = _fleet_prs(args.repo)
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(f"gh call failed: {exc.stderr}\n")
        sys.exit(1)

    fleet_state = _fleet_in_flight_from_state()
    by_issue: dict[int, dict] = {row["number"]: row for row in locks}
    for issue_str, runs in fleet_state.items():
        try:
            n = int(issue_str)
        except ValueError:
            continue
        row = by_issue.setdefault(n, {"number": n, "title": "", "locks": []})
        pids: list[dict] = []
        for run in runs:
            pid = int(run.get("pid", 0))
            if not pid:
                continue
            pids.append({
                "pid": pid,
                "persona": run.get("persona", ""),
                "pid_alive": _pid_alive(pid),
            })
        if pids:
            row["runs"] = pids
            first = pids[0]
            row["pid"] = first["pid"]
            row["persona"] = first["persona"]
            row["pid_alive"] = first["pid_alive"]
    in_flight = list(by_issue.values())

    if args.json:
        json.dump(
            {
                "repo": args.repo,
                "in_flight": in_flight,
                "fleet_prs": prs,
            },
            sys.stdout, indent=2,
        )
        sys.stdout.write("\n")
        return

    print(f"Repo: {args.repo}")
    print()
    print(f"In-flight runs ({len(in_flight)}):")
    if not in_flight:
        print("  (none)")
    for row in in_flight:
        title = row.get("title") or "(title unknown)"
        print(f"  #{row['number']} {title[:70]}")
        for lock in row.get("locks", []):
            print(f"    lock: {lock}")
        for run in row.get("runs", []):
            alive = "alive" if run["pid_alive"] else "dead"
            persona = run.get("persona") or "?"
            print(f"    pid:  {run['pid']} ({alive}, persona={persona})")
        if "pid" in row and "runs" not in row:
            alive = "alive" if row["pid_alive"] else "dead"
            persona = row.get("persona") or "?"
            print(f"    pid:  {row['pid']} ({alive}, persona={persona})")
        recent = _last_agent_comment(args.repo, row["number"])
        if recent:
            print(f"    last: {recent}")
    print()
    print(f"Fleet PRs ({len(prs)}):")
    if not prs:
        print("  (none)")
    for pr in prs[:20]:
        flag = " draft" if pr.get("isDraft") else ""
        print(f"  PR #{pr['number']} [{pr['state']}{flag}] {pr['headRefName']}")
        print(f"    {pr['title'][:90]}")


if __name__ == "__main__":
    main()
