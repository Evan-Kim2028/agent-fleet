#!/bin/bash
# fastmerge_ext.sh REPO PR HEADSHA — documents-1d PRs: merge-tree + importing test suites + fence check, then merge+verify. No LLM review (documents-1d owns quality).
F=${FLEET_OPS_HOME:-$HOME/fleet/ops}; REPO=$1; PR=$2; SHA=$3; OWNER=${FLEET_GH_OWNER:-Evan-Kim2028}; SLUG=$OWNER/$REPO
ev(){ echo "$(date +%H:%M:%S) [fastmerge #$PR] $*" >> $F/events.log; }
case "$REPO" in lake-of-rage|silphcoanalytics) ;; *) echo "usage: fastmerge_ext.sh REPO PR HEADSHA (REPO=$REPO invalid)" >&2; exit 64;; esac
[[ "$PR" =~ ^[0-9]+$ && "$SHA" =~ ^[0-9a-f]{7,40}$ ]] || { echo "usage: bad PR/SHA ($PR $SHA)" >&2; exit 64; }
T=${FLEET_WT_ROOT:-$HOME/fleet/wt}/$REPO-wt-fastmerge-$PR; B=${FLEET_WT_ROOT:-$HOME/fleet/wt}/$REPO-wt-fleetbase; [ -d $B ] || B=$(ls -d ${FLEET_WT_ROOT:-$HOME/fleet/wt}/$REPO-wt-fb-* | head -1)
git -C $B fetch -q origin; git -C $B worktree add -q --detach $T $SHA 2>/dev/null || git -C $T checkout -q --detach $SHA
cd $T || { ev "STOP cannot enter worktree $T"; exit 65; }; files=$(git diff --name-only origin/main...$SHA)
echo "$files" | grep -qE "print_identity\.py|interpret_stamps\.py|build_card_rollup_full\.py|run_prod\.sh|\.github/workflows" && { ev "STOP fenced file in diff"; exit 3; }
git -c user.name=fastmerge -c user.email=fastmerge@local merge -q --no-ff --no-commit origin/main >/dev/null 2>&1 || { c=$(git diff --name-only --diff-filter=U | tr "\n" " "); git merge --abort 2>/dev/null; ev "STOP merge conflict with main: ${c:-unknown}"; exit 4; }
mods=$(echo "$files" | grep '\.py$' | grep -v '/tests\?/' | sed 's#.*/src/##;s#\.py$##;s#/#.#g' | grep -v '^$')
tests=$(for m in $mods; do grep -rlE "(from|import) ${m}( |$|\.)" --include='test_*.py' . 2>/dev/null; done; echo "$files" | grep -E 'test_.*\.py$')
tests=$(echo "$tests" | sort -u | grep -v '^$' | head -60)
if [ -n "$tests" ]; then
  pkgdir=$( [ $REPO = silphcoanalytics ] && echo api || echo . )
  full=$($F/fm_pytest.sh $T $(echo $tests)); prc=$?; out=$(echo "$full" | grep '^SUMMARY' | tr '\n' ' ')
  [ $prc -ge 2 ] && { ev "STOP tests could not run: $(echo "$full" | grep -E '^(INFRA|SUMMARY)' | tr '\n' ' ' | cut -c1-240)"; exit 6; }
  failed=$(echo "$full" | awk '/^FAILED /{print $2}' | sort -u)
  if [ -n "$failed" ]; then
    BT=$T-base; git -C $B worktree add -q --detach $BT origin/main 2>/dev/null || git -C $BT checkout -q --detach origin/main
    bfull=$($F/fm_pytest.sh $BT $(echo "$failed" | sed 's/::.*//' | sort -u)); bfailed=$(echo "$bfull" | awk '/^FAILED /{print $2}' | sort -u); git -C $B worktree remove --force $BT 2>/dev/null
    new=$(comm -23 <(echo "$failed") <(echo "$bfailed") | grep -v '^$')
    [ -n "$new" ] && { ev "STOP new failures vs main: $(echo $new | cut -c1-240)"; exit 5; }
    ev "only baseline failures (also fail on main): $(echo $failed | cut -c1-200)"
  fi
  ev "importing suites ok: $(echo $out | tail -1 | cut -c1-120)"
fi
# TEST_ONLY=1: stop after the merged-tree test check (used to carry an approval across a patch-identical rebase).
if [ -n "$TEST_ONLY" ]; then git merge --abort 2>/dev/null; cd /; git -C $B worktree remove --force $T 2>/dev/null; exit 0; fi
if [ $REPO = lake-of-rage ]; then $F/lake_merge_verify.sh $PR ${SHA:0:9} > $F/runs/.lmv-$PR.log 2>&1; else FORCE_MERGE=1 $F/silph_merge_verify.sh $PR ${SHA:0:9} > $F/runs/.smv-$PR.log 2>&1; fi
ev "merge+verify rc=$?"; git -C $B worktree remove --force $T 2>/dev/null
