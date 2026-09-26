#!/usr/bin/env bash
# silph_merge_verify.sh PR APPROVED_SHA9 — merge one silph PR, wait for deploy.yml, verify per documents-26 procedure. Non-zero exit = stop the queue.
set -uo pipefail
PR=$1; WANT=$2; OWNER=${FLEET_GH_OWNER:-Evan-Kim2028}; REPO=$OWNER/silphcoanalytics
F=${FLEET_OPS_HOME:-$HOME/fleet/ops}
log(){ echo "$(date +%H:%M:%S) [silph-merge #$PR] $*" | tee -a $F/events.log; }
if [ -e $F/batch_mode ] && [ -z "${FORCE_MERGE:-}" ]; then echo "$PR $WANT" >> $F/batch_ready.txt; log "DEFERRED to batch (approved $WANT)"; echo "DEFERRED $WANT"; exit 0; fi
exec 9>$F/silph_merge.lock; flock -n 9 || { log "waiting for another silph merge to finish (mutex)"; flock 9; }
cd /tmp
read -r head mergeable < <(gh pr view $PR -R $REPO --json headRefOid,mergeable --jq '"\(.headRefOid[:9]) \(.mergeable)"')
for i in 1 2 3 4 5 6; do [ -n "$head" ] && [ "$mergeable" != UNKNOWN ] && break; sleep 10; read -r head mergeable < <(gh pr view $PR -R $REPO --json headRefOid,mergeable --jq '"\(.headRefOid[:9]) \(.mergeable)"'); done
# An empty answer is a GitHub/network failure, not a moved head (TLS timeout STOPped #3611, 2026-09-26 08:05): retryable.
[ -z "$head" ] && { log "gh returned no head (network); retry next tick"; exit 10; }
[ "$head" != "$WANT" ] && { log "STOP head $head != approved $WANT"; exit 2; }
[ "$mergeable" != MERGEABLE ] && { log "STOP mergeable=$mergeable (needs rebase)"; exit 3; }
lake_quiet(){ [ ! -e $F/lake_deploying ] || return 1; [ ! -e $F/silph_hold ] || return 1; local now; now=$(date +%s); [ "$now" -ge "$(cat $F/hold_silph_until 2>/dev/null || echo 0)" ] || return 1
  gh run list -R $OWNER/lake-of-rage --workflow deploy-lor-api.yml -L 5 --json status,updatedAt --jq '.[]|"\(.status) \(.updatedAt)"' 2>/dev/null | while read -r s u; do [ "$s" != completed ] && echo busy && break; [ $(( now - $(date -d "$u" +%s) )) -lt 300 ] && echo busy && break; done | grep -q busy && return 1; return 0; }
echo $$ > $F/silph_waiting; trap '[ "$(cat $F/silph_waiting 2>/dev/null)" = "$$" ] && rm -f $F/silph_waiting' EXIT
waited=0; until lake_quiet; do [ $waited = 0 ] && log "holding: lor-api deploy in progress or <5 min ago (silph_waiting set; next lake merge yields)"; waited=$((waited+30)); [ $waited -ge 10800 ] && { log "STOP lake guard held >3h"; exit 10; }; sleep 30; done
rm -f $F/silph_waiting
[ $waited -gt 0 ] && log "lake quiet after ${waited}s; merging"
gh pr merge $PR -R $REPO --merge >/dev/null 2>&1 || { log "STOP merge command failed"; exit 4; }
msha=$(gh pr view $PR -R $REPO --json mergeCommit --jq '.mergeCommit.oid'); log "merged ${msha:0:9}"
run=""; for i in $(seq 1 20); do run=$(gh run list -R $REPO --workflow deploy.yml --limit 5 --json databaseId,headSha --jq ".[] | select(.headSha==\"$msha\") | .databaseId" | head -1); [ -n "$run" ] && break; sleep 15; done
[ -z "$run" ] && { log "STOP no deploy run found for ${msha:0:9}"; exit 5; }
log "deploy run $run"
for i in $(seq 1 60); do st=$(gh run view $run -R $REPO --json status --jq .status); [ "$st" = completed ] && break; sleep 30; done
concl=$(gh run view $run -R $REPO --json conclusion --jq .conclusion)
dl=$(gh run view $run -R $REPO --log 2>/dev/null | grep -oE "Deploy (SUCCESS|INCOMPLETE)[^\"]{0,80}" | tail -1)
log "deploy conclusion=$concl marker=[${dl:-none}]"
case "$dl" in "Deploy SUCCESS"*) ;; *) log "STOP deploy did not end in Deploy SUCCESS"; exit 6;; esac
out=$(ssh lake-vps 'fe=$(grep -oE "server 127\.0\.0\.1:300[01];" /etc/nginx/sites-enabled/silphco | grep -oE "300[01]" | head -1); api=$((fe+5000)); if [ "$fe" = 3000 ]; then idle_api=silphco-api-staging; idle_fe=silphco-frontend-staging; else idle_api=silphco-api; idle_fe=silphco-frontend; fi; sha=$(curl -s --max-time 10 127.0.0.1:$api/health/data | python3 -c "import json,sys; print(json.load(sys.stdin).get(\"git_sha\",\"\"))"); pub=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 https://silphcoanalytics.xyz/health); echo "fe=$fe api=$api sha=$sha public=$pub idle_api=$(systemctl --user is-active $idle_api)/$(systemctl --user is-enabled $idle_api) idle_fe=$(systemctl --user is-active $idle_fe)/$(systemctl --user is-enabled $idle_fe)"')
log "verify: $out"
echo "$out" | grep -q "sha=${msha:0:9}" || { log "STOP serving sha mismatch"; exit 7; }
echo "$out" | grep -q "public=200" || { log "STOP public health not 200"; exit 8; }
echo "$out" | grep -qE "idle_api=(inactive|failed)/disabled idle_fe=(inactive|failed)/disabled" || { log "STOP idle color not stopped+disabled"; exit 9; }
log "VERIFIED ${msha:0:9} on :$(echo "$out" | grep -oE 'api=[0-9]+' | cut -d= -f2)"
echo "OK ${msha:0:9}"
