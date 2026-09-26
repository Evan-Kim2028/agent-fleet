#!/usr/bin/env python3
"""close_issues.py [--apply] — close issues that merged PRs declare fixed.

Fleet PRs write "Closes silph#N" / "Fixes lake#N". GitHub ignores that shorthand (it only honours #N and
owner/repo#N), so no fleet merge ever closed an issue. This reads every MERGED PR in the fleet repos,
takes its Closes/Fixes/Resolves claims (lake#N, silph#N, #N = same repo, owner/repo#N) and closes the
issue when:
  - it is still open and not an umbrella/epic/tracking issue (those span more work than one PR), and
  - no still-OPEN PR also claims to close it (split work: wait for the last part).
"Refs"/"Part of" mentions never close anything. Idempotent; one PR listing per repo per run.
"""

import collections
import datetime
import json
import os
import re
import subprocess
import sys

F = os.environ.get("FLEET_OPS_HOME") or os.path.expanduser("~/fleet/ops")
OWNER = os.environ.get("FLEET_GH_OWNER", "Evan-Kim2028")
ALIAS = {
    "lake": "lake-of-rage",
    "lake-of-rage": "lake-of-rage",
    "silph": "silphcoanalytics",
    "silphcoanalytics": "silphcoanalytics",
    "fleet": "agent-fleet",
    "agent-fleet": "agent-fleet",
}
REPOS = ["lake-of-rage", "silphcoanalytics", "agent-fleet"]
CLAIM = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+"
    r"((?:(?:[\w.-]+/)?[\w.-]+)?#\d+(?:\s*(?:,|and|&)\s*(?:(?:[\w.-]+/)?[\w.-]+)?#\d+)*)"
)
REF = re.compile(r"(?:(?:([\w.-]+)/)?([\w.-]+))?#(\d+)")
UMBRELLA = re.compile(r"(?i)^\s*\[?(umbrella|epic|tracking|meta)\b")
apply = "--apply" in sys.argv


def gh(*a):
    return json.loads(
        subprocess.run(
            ["gh", *a], capture_output=True, text=True, check=True, timeout=240
        ).stdout
    )


def ev(msg):
    with open(F + "/events.log", "a") as fh:
        fh.write(f"{datetime.datetime.now():%H:%M:%S} [close-issues] {msg}\n")


def claims(pr_repo, text):
    for m in CLAIM.finditer(text or ""):
        for owner, name, num in REF.findall(m.group(1)):
            if owner and owner != OWNER:
                continue
            repo = ALIAS.get(name, None) if name else pr_repo
            if repo:
                yield repo, int(num)


merged = collections.defaultdict(set)  # (repo, n) -> {"repo#pr"}
pending = set()  # (repo, n) still claimed by an open PR
for repo in REPOS:
    for p in gh(
        "pr",
        "list",
        "-R",
        f"{OWNER}/{repo}",
        "--state",
        "all",
        "--limit",
        "1500",
        "--json",
        "number,state,title,body",
    ):
        for target in set(claims(repo, (p["title"] or "") + "\n" + (p["body"] or ""))):
            if p["state"] == "MERGED":
                merged[target].add(
                    p["number"]
                    if target[0] == repo
                    else f"{OWNER}/{repo}#{p['number']}"
                )
            elif p["state"] == "OPEN":
                pending.add(target)

n_closed = 0
for repo in REPOS:
    open_issues = {
        i["number"]: i["title"]
        for i in gh(
            "issue",
            "list",
            "-R",
            f"{OWNER}/{repo}",
            "--state",
            "open",
            "--limit",
            "2000",
            "--json",
            "number,title",
        )
    }
    for (r, n), prs in sorted(merged.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        if (
            r != repo
            or n not in open_issues
            or (r, n) in pending
            or UMBRELLA.match(open_issues[n])
        ):
            continue
        links = ", ".join(
            f"#{x}" if isinstance(x, int) else x for x in sorted(prs, key=str)
        )
        print(f"{repo}#{n} <- {links} | {open_issues[n][:70]}")
        if not apply:
            continue
        res = subprocess.run(
            [
                "gh",
                "issue",
                "close",
                str(n),
                "-R",
                f"{OWNER}/{repo}",
                "--reason",
                "completed",
                "--comment",
                f"Fixed by {links} (merged; the PR declares it closes this issue). "
                "Closed automatically by the fleet; reopen if it still reproduces.",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if res.returncode == 0:
            n_closed += 1
            ev(f"closed {repo}#{n} (by {links})")
        else:
            ev(f"close {repo}#{n} failed: {res.stderr.strip()[:160]}")
if apply:
    print(f"closed {n_closed}")
