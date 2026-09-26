#!/bin/bash
# Merge several approved lake PRs back-to-back at their approved heads, then one full lake_merge_verify on the last.
F=${FLEET_OPS_HOME:-$HOME/fleet/ops}; OWNER=${FLEET_GH_OWNER:-Evan-Kim2028}; R=$OWNER/lake-of-rage
# Lake merge hold (prod break on main): while $F/lake_hold exists only LAKE_HOLD_OVERRIDE=1 (the hotfix) may merge.
if [ -e $F/lake_hold ] && [ -z "${LAKE_HOLD_OVERRIDE:-}" ]; then echo "$(date +%H:%M:%S) [lake-hold] $(basename $0) $* skipped: $(cat $F/lake_hold)" >> $F/events.log; exit 7; fi
ev(){ echo "$(date +%H:%M:%S) [lake-batch] $*" | tee -a $F/events.log; }
last=""; lastsha=""
for pair in "$@"; do pr=${pair%%:*}; want=${pair##*:}
  head=$(gh pr view $pr -R $R --json headRefOid,mergeable,state --jq '.headRefOid[0:9]+" "+.mergeable+" "+.state')
  set -- $head; h=$1; m=$2; st=$3
  [ "$st" = MERGED ] && { ev "#$pr already merged"; continue; }
  [ "$h" = "$want" ] || { ev "SKIP #$pr head $h != approved $want"; continue; }
  if gh pr diff $pr -R $R --name-only 2>/dev/null | grep -qE '^(transform/|orchestration/)'; then
    pc=$($F/dbt_parse_check.sh $(gh pr view $pr -R $R --json headRefOid --jq .headRefOid) 2>&1) || { ev "SKIP #$pr dbt parse check failed: $(echo "$pc" | tail -n +2 | tr '\n' ' ' | cut -c1-200)"; continue; }
  fi
  if [ -n "$last" ]; then
    gh pr merge $last -R $R --merge >/dev/null 2>&1 && ev "merged #$last (verify deferred to batch end)" || ev "FAILED merge #$last"
    sleep 20
  fi
  last=$pr; lastsha=$want
done
[ -n "$last" ] && { ev "final #$last with full lake_merge_verify"; $F/lake_merge_verify.sh $last $lastsha && ev "BATCH DONE"; }
