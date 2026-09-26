#!/bin/bash
# silph_batch_merge.sh PR:sha9 ... — merge approved silph PRs back-to-back (deploy.yml queues and only the main tip deploys),
# full silph_merge_verify (deploy + prod verify) on the LAST one. Skips stale/non-mergeable PRs.
F=${FLEET_OPS_HOME:-$HOME/fleet/ops}; OWNER=${FLEET_GH_OWNER:-Evan-Kim2028}; R=$OWNER/silphcoanalytics
ev(){ echo "$(date +%H:%M:%S) [silph-batch] $*" >> $F/events.log; }
ok=(); for pair in "$@"; do pr=${pair%%:*}; want=${pair##*:}
  read -r h m st < <(gh pr view $pr -R $R --json headRefOid,mergeable,state --jq '"\(.headRefOid[:9]) \(.mergeable) \(.state)"')
  [ "$st" = MERGED ] && { ev "#$pr already merged"; continue; }
  [ "$h" = "$want" ] && [ "$m" = MERGEABLE ] || { ev "SKIP #$pr head=$h want=$want mergeable=$m"; continue; }
  ok+=("$pr:$want"); done
[ ${#ok[@]} -eq 0 ] && { ev "nothing to merge"; exit 0; }
last=${ok[-1]}
exec 9>$F/silph_merge.lock; flock 9
for item in "${ok[@]:0:${#ok[@]}-1}"; do pr=${item%%:*}; gh pr merge $pr -R $R --merge >/dev/null 2>&1 && ev "merged #$pr (deploy collapses into batch)" || ev "FAILED merge #$pr"; done
flock -u 9
ev "final #${last%%:*} with full deploy+verify"
FORCE_MERGE=1 $F/silph_merge_verify.sh ${last%%:*} ${last##*:} > $F/runs/.silph-batch.log 2>&1; rc=$?
ev "BATCH ${ok[*]} rc=$rc"; exit $rc
