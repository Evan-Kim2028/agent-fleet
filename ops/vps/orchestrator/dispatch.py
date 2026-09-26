#!/usr/bin/env python3
"""dispatch.py QUEUE.jsonl [--max N] — run triaged items as `fleet lane run` lanes (operator documents-0e).
Keeps <= N lanes in flight; an item waits until every depends_on ref has a merged/approved lane.
Status per lane -> fb/lanes/<lane>.status (automerge2 batches approved lanes)."""

import json
import re
import os
import subprocess
import sys
import time
import pathlib

F = pathlib.Path(os.environ.get("FLEET_OPS_HOME") or os.path.expanduser("~/fleet/ops"))
WTR = pathlib.Path(os.environ.get("FLEET_WT_ROOT") or os.path.expanduser("~/fleet/wt"))
OWNER = os.environ.get("FLEET_GH_OWNER", "Evan-Kim2028")
REPOS = {
    "lake-of-rage": str(WTR / "lake-of-rage-wt-fleetbase"),
    "silphcoanalytics": str(WTR / "silphcoanalytics-wt-fleetbase"),
    "agent-fleet": str(WTR / "agent-fleet-wt-release"),
}
# Base checkouts are dedicated detached worktrees nobody merges or cleans up; refuse to start without them.
missing = [p for p in REPOS.values() if not pathlib.Path(p).is_dir()]
if missing:
    sys.exit(f"dispatch: missing base repo path(s): {missing}")
FENCE = (F / "prompts" / "fences.md").read_text()
queue = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
maxn = int(sys.argv[sys.argv.index("--max") + 1]) if "--max" in sys.argv else 20
running: dict[str, subprocess.Popen] = {}
# --adopt FILE: lanes an earlier dispatcher launched and that are still running; watched via ps, then gated like ours.
adopted: set[str] = (
    set(json.load(open(sys.argv[sys.argv.index("--adopt") + 1])))
    if "--adopt" in sys.argv
    else set()
)
GMAX = (
    int(sys.argv[sys.argv.index("--max-gates") + 1])
    if "--max-gates" in sys.argv
    else 10
)
maxload = (
    float(sys.argv[sys.argv.index("--max-load") + 1])
    if "--max-load" in sys.argv
    else 64.0
)


REPO_SLUG = {
    "lake": f"{OWNER}/lake-of-rage",
    "silph": f"{OWNER}/silphcoanalytics",
    "fleet": f"{OWNER}/agent-fleet",
}


def pr_link(ref):
    """PR-body instruction that links the issue in a form GitHub honours (it ignores the lake#N/silph#N shorthand)."""
    m = re.fullmatch(r"(lake|silph|fleet)#(\d+)", str(ref))
    if not m:
        return f"Reference {ref} in the PR body."
    full = f"{REPO_SLUG[m.group(1)]}#{m.group(2)}"
    return (
        f"The PR body MUST contain the exact line `Closes {full}` when this PR fully fixes the issue, "
        f"or `Refs {full}` when it is only part of it (never the lake#/silph# shorthand: GitHub will not close the issue)."
    )


def lane_alive(lane: str) -> bool:
    return (
        f"--lane {lane} "
        in subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
    )


def lookup(lane: str) -> dict | None:
    """Queue item for *lane*: this queue, the original queue, else the repo from its fleet run log."""
    for src in (
        queue,
        [json.loads(x) for x in open(F / "triage" / "queue-0e.jsonl") if x.strip()],
    ):
        for q in src:
            if q["lane"] == lane:
                return q
    try:
        raw = (F / "runs" / f"lane-{lane}.log").read_text()
        d = json.loads(raw[raw.index("{") :])
        repo = next(r for r in REPOS if r in (d.get("worktree") or ""))
        return {"lane": lane, "repo": repo, "ref": lane}
    except Exception:
        return None


class _Adopted:
    def __init__(self, lane: str) -> None:
        self.lane = lane

    def poll(self):
        return None if lane_alive(self.lane) else 0


for _l in adopted:
    running[_l] = _Adopted(_l)  # type: ignore[assignment]
gates: dict[str, subprocess.Popen] = {}
gate_q: list[tuple[str, str, str]] = []
done: dict[str, str] = {}


def ev(msg: str) -> None:
    with open(F / "events.log", "a") as fh:
        fh.write(time.strftime("%H:%M:%S") + f" [dispatch] {msg}\n")


def status_of(lane: str) -> str:
    p = F / "lanes" / f"{lane}.status"
    return (
        p.read_text().strip().splitlines()[-1]
        if p.exists() and p.read_text().strip()
        else ""
    )


pending = [it for it in queue if it["lane"] not in adopted]
# Fence screen: a lane whose files/task need a fenced path can only stop without a PR; route it instead of launching.
import re as _re

_fences = (
    [
        l.split("\t")
        for l in (F / "fences.re").read_text().splitlines()
        if l.strip() and not l.startswith("#")
    ]
    if (F / "fences.re").exists()
    else []
)


def _route(it):
    blob = " ".join(it.get("files") or []) + " " + it.get("task", "")
    for pat, route in _fences:
        if _re.search(pat, blob):
            return route, pat
    return None


_kept = []
for it in pending:
    r = _route(it)
    if r is None:
        _kept.append(it)
        continue
    with open(F / "triage" / f"routed-{r[0]}.jsonl", "a") as fh:
        fh.write(json.dumps({**it, "routed_because": r[1]}) + "\n")
    with open(F / "events.log", "a") as fh:
        fh.write(
            time.strftime("%H:%M:%S")
            + f" [dispatch] routed {it['lane']} -> {r[0]} (fence {r[1]})\n"
        )
pending = _kept
while pending or running or gates or gate_q:
    for lane, proc in list(running.items()):
        if proc.poll() is not None:
            del running[lane]
            fleet_verdict = (
                (F / "fleetlane" / f"{lane}.status")
                .read_text()
                .strip()
                .splitlines()[-1:]
                if (F / "fleetlane" / f"{lane}.status").exists()
                else []
            )
            it = lookup(lane)
            if it is None:
                ev(
                    f"{lane} finished but is in no queue and its run log names no repo; not gated"
                )
                done[lane] = "UNKNOWN"
                continue
            pr = subprocess.run(
                [
                    "gh",
                    "pr",
                    "list",
                    "-R",
                    f"{OWNER}/{it['repo']}",
                    "--head",
                    f"fb/{lane}",
                    "--state",
                    "open",
                    "--json",
                    "number",
                    "--jq",
                    ".[0].number",
                ],
                capture_output=True,
                text=True,
            ).stdout.strip()
            ev(
                f"{lane} fleet lane done (lane status: {(fleet_verdict or ['none'])[0][:90]}); PR #{pr or '-'}"
            )
            if pr:
                # AUTHORITATIVE review = bash fbgate until fleet gate passes the A/B (agent-fleet #101).
                # Non-blocking: queue the gate; lanes keep launching while gates are at their cap.
                gate_q.append((lane, it["repo"], pr))
            else:
                done[lane] = "NO-PR"
    while gate_q and len(gates) < GMAX:
        _l, _r, _pr = gate_q.pop(0)
        gates[_l] = subprocess.Popen(
            [str(F / "fbgate"), _l, _r, _pr],
            stdout=open(F / f"gate-{_l}.log", "w"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        ev(f"{_l} fbgate started (queued gates left {len(gate_q)})")
    for lane, g in list(gates.items()):
        if g.poll() is not None:
            del gates[lane]
            done[lane] = status_of(lane) or f"gate exit {g.returncode}"
            ev(f"{lane} fbgate: {done[lane][:120]}")
    # A dependency is released once its lane is terminal (approved OR escalated): depends_on only
    # serialises lanes that touch the same files, and an escalated lane must not deadlock its chain.
    ready_refs = {it["ref"] for it in queue if it["lane"] in done}
    # Network backpressure: one quick probe per tick; a saturated home uplink kills agents ("Unable to connect").
    _net = subprocess.run(
        [
            "curl",
            "-s",
            "-o",
            "/dev/null",
            "-w",
            "%{time_connect}",
            "--max-time",
            "6",
            "https://api.github.com",
        ],
        capture_output=True,
        text=True,
    ).stdout.strip()
    try:
        net_ok = 0 < float(_net or 0) < 3
    except ValueError:
        net_ok = False
    for it in list(pending):
        if not net_ok and it.get("cluster") != "C0":
            break
        if it.get("cluster") != "C0" and (
            len(running) >= maxn or os.getloadavg()[0] > maxload
        ):
            break
        if any(
            d not in ready_refs and any(q["ref"] == d for q in queue)
            for d in it.get("depends_on") or []
        ):
            continue
        lane, repo = it["lane"], it["repo"]
        task = F / "prompts" / f"{lane}.task.md"
        task.write_text(
            f"# Lane {lane} ({it['ref']}, {repo}, {it['area']}, size {it['size']})\n\n{it['task']}\n\nEvidence from triage: {it.get('evidence', '')}\nFiles: {', '.join(it.get('files') or [])}\ndbt models: {', '.join(it.get('dbt_models') or [])}\n\nRULES: targeted tests only, run them memory-capped; commit (never --no-verify), push, open ONE PR. {pr_link(it['ref'])} Never kill processes by name/pattern.\n\n===== STANDING FENCES =====\n{(F / 'prompts' / 'fences.md').read_text()}\n"
        )
        cmd = [
            "fleet",
            "lane",
            "run",
            "--operator",
            "documents-0e",
            "--lane",
            lane,
            "--repo-path",
            REPOS[repo],
            "--task-file",
            str(task),
            "--engine",
            "cmd",
            "--expected-repo",
            f"{OWNER}/{repo}",
            "--status-file",
            str(F / "fleetlane" / f"{lane}.status"),
            "--no-gate",
            "--json",
        ]
        log = open(F / "runs" / f"lane-{lane}.log", "w")
        running[lane] = subprocess.Popen(
            cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "PATH": f"{F}/shim:" + os.environ.get("PATH", "")},
        )
        pending.remove(it)
        ev(f"launched {lane} ({it['ref']}) via fleet lane run")
    time.sleep(20)
ev(f"queue {sys.argv[1]} complete: {len(done)} lanes")
