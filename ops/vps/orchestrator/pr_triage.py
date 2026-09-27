#!/usr/bin/env python3
"""pr_triage.py [--apply] — deterministic (no model) triage of OPEN fb/* PRs in lake-of-rage and silphcoanalytics.

Decision per PR, first match wins:
  ON-MAIN      the change is already in origin/main (git cherry shows no '+' commit, or the 3-dot diff is empty) -> close
  SUPERSEDED   another OPEN fb/ PR for the same issue ref contains every commit of this one (git cherry), or a MERGED
               PR merged after this PR was opened declares Closes/Fixes for the same issue ref, that issue is CLOSED, and the ref is not split
               across several lanes -> close
  DEAD         last status is a real (non-infra) escalation, re-gated >= 2x at this head, no live gate, PR > 24h old -> close
  CONFLICTING  mergeable=CONFLICTING, no live gate or rebase for the lane -> launch rebase_regate.sh (<= 4 concurrent)
  KEEP         everything else
Other head prefixes (dq1d/, feat/, fix/, p1b/, ...) are only counted, never touched.
Dry-run by default; --apply acts. Every action is logged to events.log as "[pr-triage] ...".
"""
import collections
import datetime
import glob
import json
import os
import re
import subprocess
import sys

F = os.environ.get("FLEET_OPS_HOME") or os.path.expanduser("~/fleet/ops")
WT_ROOT = os.environ.get("FLEET_WT_ROOT") or os.path.expanduser("~/fleet/wt")
OWNER = os.environ.get("FLEET_GH_OWNER", "Evan-Kim2028")
REPOS = {"lake-of-rage": "lake", "silphcoanalytics": "silph"}
REAL_VERDICT = re.compile(r"no-push after|fix round pushed nothing|after one fix round|untestable-unresolved|merged-tree regression")
CLAIM = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+((?:[\w.-]+/)?(?:[\w.-]+)?#\d+)")
MAX_REBASES = 4
APPLY = "--apply" in sys.argv
NOW = datetime.datetime.now(datetime.timezone.utc)


def sh(*a, check=False, timeout=300):
    r = subprocess.run(list(a), capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f"{a[:4]} rc={r.returncode}: {r.stderr[:300]}")
    return r


def gh_json(*a):
    for _ in range(4):
        r = sh("gh", *a, timeout=240)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    raise RuntimeError(f"gh {' '.join(a[:3])} failed: {r.stderr[:200]}")


def ev(msg):
    with open(F + "/events.log", "a") as fh:
        fh.write(f"{datetime.datetime.now():%H:%M:%S} [pr-triage] {msg}\n")


def lane_refs():
    lanes = collections.defaultdict(set)   # lane -> {refs}
    per_ref = collections.defaultdict(set)  # ref -> {lanes}
    for f in glob.glob(F + "/triage/*.jsonl"):
        for line in open(f):
            if not line.strip():
                continue
            try:
                i = json.loads(line)
            except json.JSONDecodeError:
                continue
            lane, ref = i.get("lane"), str(i.get("ref") or "")
            if lane and re.fullmatch(r"(lake|silph)#\d+", ref):
                lanes[lane].add(ref)
                per_ref[ref].add(lane)
    return lanes, per_ref


def processes():
    out = sh("ps", "-eo", "args").stdout.splitlines()
    gating, rebasing = set(), set()
    for a in out:
        t = a.split()
        for k, w in enumerate(t):
            if (w == "fbgate" or w.endswith("/fbgate")) and k + 1 < len(t):
                gating.add(t[k + 1])
            if w.endswith("rebase_regate.sh") and k + 1 < len(t):
                rebasing.add(t[k + 1])
    return gating, rebasing


def regate_count(lane, head9):
    n = 0
    for f in ("requeued_failclosed.txt", "reworked.txt"):
        try:
            n += sum(1 for l in open(os.path.join(F, f)) if l.strip() == f"{lane} {head9}")
        except FileNotFoundError:
            pass
    return n


def last_escalation(lane):
    try:
        lines = open(f"{F}/lanes/{lane}.status").read().splitlines()
    except FileNotFoundError:
        return ""
    rel = [l for l in lines if re.search(r"PREMERGE-APPROVED|NEEDS-ESCALATION|NEEDS-REBASE| start @|re-gate", l)]
    return rel[-1] if rel and "NEEDS-ESCALATION" in rel[-1] else ""


def blockers(lane):
    out = []
    try:
        for l in open(f"{F}/gate/{lane}/confirmed.jsonl"):
            if l.strip():
                c = json.loads(l)
                out.append(f"- {c.get('file', '?')}: {str(c.get('claim', '')).strip()[:180]}")
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return out[:5]


def issue_closed(repo, n, cache={}):
    k = (repo, n)
    if k not in cache:
        r = sh("gh", "issue", "view", str(n), "-R", f"{OWNER}/{repo}", "--json", "state", "--jq", ".state")
        cache[k] = r.stdout.strip() == "CLOSED"
    return cache[k]


def main():
    lanes, per_ref = lane_refs()
    gating, rebasing = processes()
    rebases_running = len(rebasing)
    rows, other = [], collections.Counter()
    for repo, short in REPOS.items():
        slug = f"{OWNER}/{repo}"
        base = os.path.join(WT_ROOT, f"{repo}-wt-fleetbase")
        prs = gh_json("pr", "list", "-R", slug, "--state", "all", "--limit", "1500",
                      "--json", "number,state,headRefName,headRefOid,title,body,mergeable,createdAt,mergedAt")
        open_fb = [p for p in prs if p["state"] == "OPEN" and p["headRefName"].startswith("fb/")]
        for p in prs:
            if p["state"] == "OPEN" and not p["headRefName"].startswith("fb/"):
                other[(repo, p["headRefName"].split("/")[0])] += 1
        # merged PRs' Closes/Fixes claims for this repo's issues (lake#N / silph#N / #N same repo)
        merged_claims = collections.defaultdict(set)
        for p in prs:
            if p["state"] != "MERGED":
                continue
            for m in CLAIM.finditer((p["title"] or "") + "\n" + (p["body"] or "")):
                ref = m.group(1)
                num = int(ref.split("#")[1])
                pre = ref.split("#")[0].split("/")[-1]
                tgt = {"": short, "lake": "lake", "lake-of-rage": "lake", "silph": "silph", "silphcoanalytics": "silph"}.get(pre)
                if tgt:
                    merged_claims[f"{tgt}#{num}"].add((p["number"], p["mergedAt"] or ""))
        sh("git", "-C", base, "fetch", "-q", "origin", "main")
        refspecs = [f"+refs/pull/{p['number']}/head:refs/remotes/pr/{p['number']}" for p in open_fb]
        for i in range(0, len(refspecs), 50):
            sh("git", "-C", base, "fetch", "-q", "origin", *refspecs[i:i + 50], timeout=600)
        # mergeable is often UNKNOWN in list output; ask per PR (this also triggers GitHub's computation)
        for p in open_fb:
            if p["mergeable"] == "UNKNOWN":
                r = sh("gh", "pr", "view", str(p["number"]), "-R", slug, "--json", "mergeable", "--jq", ".mergeable")
                p["mergeable"] = r.stdout.strip() or "UNKNOWN"
        by_ref_open = collections.defaultdict(list)
        for p in open_fb:
            for ref in lanes.get(p["headRefName"][3:], ()):
                by_ref_open[ref].append(p)

        for p in open_fb:
            n, lane, head9 = p["number"], p["headRefName"][3:], p["headRefOid"][:9]
            pr_ref = f"refs/remotes/pr/{n}"
            age_h = (NOW - datetime.datetime.fromisoformat(p["createdAt"].replace("Z", "+00:00"))).total_seconds() / 3600
            decision, evidence = "KEEP", ""
            cherry = sh("git", "-C", base, "cherry", "origin/main", pr_ref)
            empty = sh("git", "-C", base, "diff", "--quiet", f"origin/main...{pr_ref}").returncode == 0
            if cherry.returncode == 0 and (not any(l.startswith("+") for l in cherry.stdout.splitlines()) or empty):
                decision = "ON-MAIN"
                evidence = ("three-dot diff against origin/main is empty" if empty else
                            "every commit is already on origin/main (git cherry: " +
                            ", ".join(l.split()[1][:9] for l in cherry.stdout.splitlines() if l.startswith("-"))[:200] + ")")
            if decision == "KEEP":
                for ref in sorted(lanes.get(lane, ())):
                    for q in by_ref_open.get(ref, []):
                        if q["number"] == n:
                            continue
                        c1 = sh("git", "-C", base, "cherry", f"refs/remotes/pr/{q['number']}", pr_ref)
                        if c1.returncode or any(l.startswith("+") for l in c1.stdout.splitlines()):
                            continue
                        c2 = sh("git", "-C", base, "cherry", pr_ref, f"refs/remotes/pr/{q['number']}")
                        mutual = c2.returncode == 0 and not any(l.startswith("+") for l in c2.stdout.splitlines())
                        if mutual and n < q["number"]:
                            continue  # identical twins: keep the older PR, close the newer one
                        decision, evidence = "SUPERSEDED", f"open PR #{q['number']} ({q['headRefName']}) for the same issue {ref} contains every commit of this PR"
                        break
                    if decision != "KEEP":
                        break
            if decision == "KEEP":
                for ref in sorted(lanes.get(lane, ())):
                    # only a closer merged AFTER this PR was opened supersedes it: an older "Closes" that the fleet
                    # re-worked (e.g. silph#3065: #3908 merged 09-12, #4290 opened 09-25) did not finish the issue
                    closers = {c for c, at in merged_claims.get(ref, ()) if at > p["createdAt"]}
                    if closers and len(per_ref.get(ref, ())) == 1 and issue_closed(repo if ref.startswith(short) else
                                                                                    {"lake": "lake-of-rage", "silph": "silphcoanalytics"}[ref.split("#")[0]],
                                                                                    int(ref.split("#")[1])):
                        decision = "SUPERSEDED"
                        evidence = f"issue {ref} is closed; merged PR(s) {', '.join('#%d' % c for c in sorted(closers))} declare they close it"
                        break
            esc = last_escalation(lane)
            if decision == "KEEP" and esc and REAL_VERDICT.search(esc) and lane not in gating and age_h >= 24 \
                    and regate_count(lane, head9) >= 2:
                decision = "DEAD"
                evidence = f"escalated after {regate_count(lane, head9)} automatic re-gates at {head9}: {esc[9:160]}"
            if decision == "KEEP" and p["mergeable"] == "CONFLICTING" and lane not in gating and lane not in rebasing:
                decision, evidence = "CONFLICTING", "conflicts with main; no gate or rebase running"
            rows.append(dict(repo=repo, n=n, lane=lane, head9=head9, decision=decision, evidence=evidence,
                             mergeable=p["mergeable"], age_h=round(age_h), gating=lane in gating, esc=esc[9:90]))

    print(f"{'repo':16} {'PR':>5} {'decision':11} {'merge':11} {'age':>4}  lane | evidence")
    for r in sorted(rows, key=lambda r: (r["repo"], r["decision"], r["n"])):
        print(f"{r['repo']:16} {r['n']:>5} {r['decision']:11} {r['mergeable']:11} {r['age_h']:>4}h {r['lane']} | {r['evidence'] or r['esc']}")
    cnt = collections.Counter((r["repo"], r["decision"]) for r in rows)
    print("\nCOUNTS", dict(sorted(cnt.items())))
    print("NON-FB OPEN (reported only)", dict(sorted(other.items())))
    json.dump(rows, open(F + "/triage/pr_triage_last.json", "w"), indent=1)
    if not APPLY:
        print("\n(dry run; pass --apply to act)")
        return

    for r in rows:
        slug = f"{OWNER}/{r['repo']}"
        if r["decision"] in ("ON-MAIN", "SUPERSEDED"):
            body = (f"Closing ({r['decision']}): {r['evidence']}. Nothing in this PR is lost. "
                    "Closed by the fleet's deterministic PR triage; reopen if this is wrong.")
        elif r["decision"] == "DEAD":
            b = blockers(r["lane"])
            body = ("Closing: the evidence gate confirmed blockers that automatic fix rounds did not resolve "
                    f"({r['evidence']}).\n\nConfirmed blockers (each backed by a failing test):\n" +
                    ("\n".join(b) if b else "- (see the gate log)") +
                    "\n\nThe issue stays open for a fresh attempt from current main. The branch is kept. "
                    "Closed by the fleet's deterministic PR triage.")
        elif r["decision"] == "CONFLICTING":
            if rebases_running >= MAX_REBASES:
                continue
            log = open(f"{F}/runs/.rebase-{r['lane']}.log", "w")
            subprocess.Popen(["setsid", "nohup", f"{F}/rebase_regate.sh", r["lane"], r["repo"], str(r["n"])],
                             stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
            rebases_running += 1
            ev(f"rebase launched for {r['repo']}#{r['n']} ({r['lane']}): conflicts with main")
            continue
        else:
            continue
        res = sh("gh", "pr", "close", str(r["n"]), "-R", slug, "--comment", body)
        if res.returncode == 0:
            ev(f"closed {r['repo']}#{r['n']} ({r['lane']}) {r['decision']}: {r['evidence'][:160]}")
            print(f"closed {r['repo']}#{r['n']} {r['decision']}")
        else:
            ev(f"close {r['repo']}#{r['n']} failed: {res.stderr.strip()[:160]}")


if __name__ == "__main__":
    main()
