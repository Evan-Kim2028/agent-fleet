#!/usr/bin/env bash
# lane_rebase.sh LANE REPO PR — on-box rebase path (port of ops-side
# rebase_regate.sh for the gate_queue driver). Rebases fb/LANE (or
# dq1d/<x> for dq1d-<x> lanes) onto origin/main via an fbagent cmd call,
# pushes with --force-with-lease, then appends the lane back to the gate
# queue so the driver re-gates the new head in order.
set -uo pipefail
F=$HOME/fleet/fb; S=$F/lanes; W_ROOT=$HOME/fleet/wt
LANE=$1; REPO=$2; PR=$3
case "$LANE" in dq1d-*) BR="dq1d/${LANE#dq1d-}";; *) BR="fb/$LANE";; esac
ST(){ echo "$(date +%H:%M:%S) $*" >> $S/$LANE.status; echo "$(date +%H:%M:%S) [$LANE] $*" >> $F/events.log; }
mkdir -p $F/locks
exec 9>$F/locks/rebase-$LANE.lock
flock -n 9 || { echo "lane_rebase: $LANE locked"; exit 0; }
B=$W_ROOT/$REPO-base; [ -d $B ] || { ST "NEEDS-ESCALATION rebase: no base checkout at $B"; exit 2; }
W=$W_ROOT/$REPO-wt-rebase-$LANE
git -C $B fetch -q origin
held=$(git -C $B worktree list --porcelain | awk -v b="branch refs/heads/$BR" '/^worktree /{w=$2} $0==b{print w}')
[ -n "$held" ] && W=$held
if [ ! -d $W ]; then
  git -C $B worktree add -q -B $BR $W origin/$BR || { ST "NEEDS-ESCALATION rebase: cannot create worktree"; exit 2; }
fi
git -C $W fetch -q origin
if git -C $W status --porcelain | grep -q .; then
  # 2026-09-28: dirty lane worktrees are almost always a dead rebase agent's
  # leftovers (laptop-side attempts left several). Stash, don't refuse — the
  # work stays recoverable in the stash and the rebase proceeds.
  if pgrep -f "rebase-$LANE\|fbgate $LANE" >/dev/null 2>&1; then
    ST "NEEDS-ESCALATION rebase: worktree $W is dirty AND a process still owns it"; exit 3
  fi
  if git -C $W stash push -u -q -m "lane_rebase leftover $(date +%F_%T)"; then
    ST "rebase: stashed leftover dirty state in $W (see git stash list)"
  else
    ST "NEEDS-ESCALATION rebase: could not stash dirty worktree $W"; exit 3
  fi
fi
git -C $W reset -q --hard origin/$BR
before=$(git -C $B rev-parse origin/$BR)
# already rebased? If origin/main's tip is the merge base of the branch, a prior
# run (or a dead wrapper whose agent finished anyway) already rebased it —
# don't burn an agent, just re-queue.
mb=$(git -C $B merge-base origin/main origin/$BR)
if [ "$mb" = "$(git -C $B rev-parse origin/main)" ]; then
  ST "branch already based on current main; queueing re-gate"
  printf "%s %s %s\n" "$LANE" "$REPO" "$PR" >> $F/gate_queue.txt
  exit 0
fi
P=$F/prompts; mkdir -p $P
sed -e "s#@W@#$W#g; s#@L@#$LANE#g; s#@PR@#$PR#g; s#@REPO@#$REPO#g; s#@BR@#$BR#g; s#@OWNER@#Evan-Kim2028#g" > $P/rebase-$LANE.md <<'P'
In the worktree @W@ (branch @BR@, PR #@PR@ in @OWNER@/@REPO@): the PR conflicts with main.
Run `git fetch origin && git rebase origin/main` (or merge origin/main if the rebase is unmanageable),
resolve every conflict preserving BOTH main's changes and this PR's intent, run the targeted tests
for the touched files (memory-capped: `systemd-run --user --scope -q --slice=fleet.slice -p MemoryMax=6G -p MemorySwapMax=0 uv run pytest -q <files>`,
never the full suite), commit (never --no-verify), and push with `git push --force-with-lease`.
Do not change behaviour beyond conflict resolution. If a conflict is add/add on a gate test file
(test_gate_*.py) because another PR already merged a file with the same name, keep main's file
untouched and rename THIS PR's file to test_gate_<lane>_<rest>.py with lane=@L@ (non-alphanumerics -> _);
never edit or weaken either file's assertions. Never kill processes by name or pattern.
Final line: REBASED <new head sha> or CANNOT-REBASE <reason>.
P
ST "rebase agent starting on $W"
$F/fbagent rebase-$LANE $W $P/rebase-$LANE.md 300
git -C $B fetch -q origin; after=$(git -C $B rev-parse origin/$BR)
if [ "$after" = "$before" ]; then
  ST "NEEDS-ESCALATION rebase agent pushed nothing"; exit 5
fi
ST "rebased onto main -> ${after:0:9}; queueing re-gate"
printf "%s %s %s\n" "$LANE" "$REPO" "$PR" >> $F/gate_queue.txt
exit 0
