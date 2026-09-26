#!/bin/bash
# rebase_regate.sh LANE REPO PR — a cmd agent rebases fb/LANE onto origin/main (conflicts resolved, targeted tests),
# pushes, then fbgate re-gates the new head. Invoked by automerge2 when a merge stops on CONFLICTING (rc=3).
F=${FLEET_OPS_HOME:-$HOME/fleet/ops}
L=$1; REPO=$2; PR=$3; S=$F/lanes/$L.status; W=${FLEET_WT_ROOT:-$HOME/fleet/wt}/$REPO-wt-fb-$L
st(){ echo "$(date +%H:%M:%S) $*" >> $S; echo "$(date +%H:%M:%S) [$L] $*" >> $F/events.log; }
exec 9>$F/rebase-$L.lock; flock -n 9 || exit 0
B=${FLEET_WT_ROOT:-$HOME/fleet/wt}/$REPO-wt-fleetbase; git -C $B fetch -q origin
# Reuse whichever worktree already holds fb/$L (lane worktrees live at $REPO-wt-fleetbase-wt-fb-$L); a second
# `worktree add` of a checked-out branch fails ("already checked out").
held=$(git -C $B worktree list --porcelain | awk -v b="branch refs/heads/fb/$L" '/^worktree /{w=$2} $0==b{print w}')
[ -n "$held" ] && W=$held
if [ ! -d $W ]; then git -C $B worktree add -q -B fb/$L $W origin/fb/$L || { st "NEEDS-ESCALATION rebase: cannot create worktree"; exit 2; }; fi
git -C $W fetch -q origin
if git -C $W status --porcelain -- . ':!.agent-fleet' | grep -q .; then st "NEEDS-ESCALATION rebase: worktree $W has uncommitted changes (not touched)"; exit 3; fi
git -C $W reset -q --hard origin/fb/$L
before=$(git -C $B rev-parse origin/fb/$L)
sed -e "s#@W@#$W#g; s#@L@#$L#g; s#@PR@#$PR#g; s#@REPO@#$REPO#g; s#@OWNER@#${FLEET_GH_OWNER:-Evan-Kim2028}#g" > $F/prompts/rebase-$L.md <<'P'
In the worktree @W@ (branch fb/@L@, PR #@PR@ in @OWNER@/@REPO@): the PR conflicts with main. Run `git fetch origin && git rebase origin/main`
(or merge origin/main if the rebase is unmanageable), resolve every conflict preserving BOTH main's changes and this PR's intent, run the targeted tests
for the touched files (memory-capped, never the full suite), commit (never --no-verify), and push with `git push --force-with-lease`.
Do not change behaviour beyond conflict resolution. If a conflict is add/add on a gate test file (test_gate_*.py) because another PR already merged a file with the same name, keep main's file untouched and rename THIS PR's file to test_gate_<lane>_<rest>.py with lane=@L@ (non-alphanumerics -> _); never edit or weaken either file's assertions. Never kill processes by name/pattern. Final line: REBASED <new head sha> or CANNOT-REBASE <reason>.
P
$F/fbrun rebase-$L $W $F/prompts/rebase-$L.md
git -C $B fetch -q origin; after=$(git -C $B rev-parse origin/fb/$L)
[ "$after" = "$before" ] && { st "NEEDS-ESCALATION rebase agent pushed nothing"; exit 5; }
# Light path: if the PR's own change is textually identical after the rebase (patch-id over the diff,
# excluding gate tests whose names/conflicts are what usually forced the rebase), the approval carries
# over once the PR's tests pass merged onto current main. Anything else gets a full fbgate re-review.
pid(){ git -C $B diff $(git -C $B merge-base origin/main $1) $1 -- . ':(exclude,glob)**/test_gate_*.py' | git -C $B patch-id --stable | cut -d' ' -f1; }
git -C $B fetch -q origin
p0=$(pid $before); p1=$(pid $after)
was_approved=$(grep -E "PREMERGE-APPROVED|NEEDS-|start @" $S | grep -v NEEDS-REBASE | tail -1 | grep -c PREMERGE-APPROVED)
if [ -n "$p0" ] && [ "$p0" = "$p1" ] && [ "$was_approved" = 1 ]; then
  st "rebased onto main -> ${after:0:9}; change is patch-identical (patch-id ${p1:0:12}); re-testing on merged main instead of a full re-gate"
  if TEST_ONLY=1 $F/fastmerge_ext.sh $REPO $PR $after > $F/runs/.rr-test-$L.log 2>&1; then
    st "PREMERGE-APPROVED ${after:0:9}"; exit 0
  fi
  st "merged-main tests failed after rebase; full re-gate"
else
  st "rebased onto main -> ${after:0:9}; change differs from the approved patch (or was not approved); full re-gate"
fi
$F/fbgate $L $REPO $PR > $F/gate-$L.log 2>&1
