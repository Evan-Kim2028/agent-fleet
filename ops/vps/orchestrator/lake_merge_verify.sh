#!/usr/bin/env bash
# lake_merge_verify.sh PR APPROVED_SHA9 [search] [keep_lock] — merge one lake PR in documents-26's window, wait for deploy-lor-api + lor-main verify, probe vs baseline, check replica :8802.
set -uo pipefail
PR=$1; WANT=$2; MODE=${3:-}; KEEP=${4:-}; OWNER=${FLEET_GH_OWNER:-Evan-Kim2028}; REPO=$OWNER/lake-of-rage
F=${FLEET_OPS_HOME:-$HOME/fleet/ops}
# Lake merge hold (prod break on main): while $F/lake_hold exists only LAKE_HOLD_OVERRIDE=1 (the hotfix) may merge.
if [ -e $F/lake_hold ] && [ -z "${LAKE_HOLD_OVERRIDE:-}" ]; then echo "$(date +%H:%M:%S) [lake-hold] $(basename $0) $* skipped: $(cat $F/lake_hold)" >> $F/events.log; exit 7; fi
log(){ echo "$(date +%H:%M:%S) [lake-merge #$PR] $*" | tee -a $F/events.log; }
if [ -e $F/require_ship_$PR ]; then log "waiting for owner SHIP flag ship_$PR ($WANT)"; w=0; until [ "$(cat $F/ship_$PR 2>/dev/null)" = "$WANT" ]; do w=$((w+30)); [ $w -ge 7200 ] && { log "STOP no owner SHIP for $WANT within 2h"; exit 12; }; sleep 30; done; log "owner SHIP present for $WANT"; fi
exec 8>$F/lake_merge.lock; flock 8
w=0; while p=$(cat $F/silph_waiting 2>/dev/null) && [ -n "$p" ] && kill -0 "$p" 2>/dev/null; do [ $w = 0 ] && log "yielding to waiting silph merge (pid $p)"; w=$((w+30)); [ $w -ge 1800 ] && { log "silph still waiting after 30 min; proceeding"; break; }; sleep 30; done
touch $F/lake_deploying; cd /tmp
# Any exit (conflict rc=3, deploy failure, ...) must release the lock unless the caller keeps it; a stale lock deadlocked silph merges for 1h40m.
release(){ [ -z "$KEEP" ] && rm -f $F/lake_deploying; }; trap release EXIT
w=0; until [ -z "$(gh run list -R $OWNER/silphcoanalytics --workflow deploy.yml -L 3 --json status --jq '.[]|select(.status!="completed")|.status')" ]; do [ $w = 0 ] && log "waiting for silph deploy to finish"; w=1; sleep 30; done
w=0; until [ -z "$(ssh ${FLEET_VPS_HOST:-lake-vps-lor-main} 'systemctl --user list-units --state=active --no-legend --plain "lor-api-deploy-verify-*"; [ "$(systemctl --user is-active lor-sidecar-refresh)" = active ] && echo sidecar-refresh-active')" ]; do [ $w = 0 ] && log "waiting: lor-main deploy verify or sidecar refresh active"; w=$((w+30)); [ $w -ge 5400 ] && { log "STOP lor-main busy >90 min"; exit 11; }; sleep 30; done
read -r head mergeable < <(gh pr view $PR -R $REPO --json headRefOid,mergeable --jq '"\(.headRefOid[:9]) \(.mergeable)"')
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do [ -n "$head" ] && [ "$mergeable" != UNKNOWN ] && break; sleep 10; read -r head mergeable < <(gh pr view $PR -R $REPO --json headRefOid,mergeable --jq '"\(.headRefOid[:9]) \(.mergeable)"'); done
# An empty answer is a GitHub/network failure, not a moved head (TLS timeout STOPped #3611, 2026-09-26 08:05): retryable.
[ -z "$head" ] && { log "gh returned no head (network); retry next tick"; exit 10; }
[ "$head" != "$WANT" ] && { log "STOP head $head != approved $WANT"; exit 2; }
[ "$mergeable" != MERGEABLE ] && { log "STOP mergeable=$mergeable"; exit 3; }
t0=$(date +%s)
# dbt parse gate (transform-ci never runs under the Actions billing block; #3547 shipped a macro that broke parse on prod).
if gh pr diff $PR -R $REPO --name-only 2>/dev/null | grep -qE '^(transform/|orchestration/)'; then
  pc=$($F/dbt_parse_check.sh $(gh pr view $PR -R $REPO --json headRefOid --jq .headRefOid) 2>&1); prc=$?
  log "$(echo "$pc" | head -1)"
  [ $prc = 0 ] || { log "STOP dbt parse check failed (rc=$prc): $(echo "$pc" | tail -n +2 | tr '\n' ' ' | cut -c1-240)"; exit 8; }
fi
gh pr merge $PR -R $REPO --merge >/dev/null 2>&1 || { log "STOP merge command failed"; exit 4; }
msha=$(gh pr view $PR -R $REPO --json mergeCommit --jq '.mergeCommit.oid'); log "merged ${msha:0:9}"
# Pause ad-hoc agent jobs on lor-main (agents-adhoc.slice) while the deploy gate/audit + verify run; thaw on any exit.
thaw(){ ssh -o ConnectTimeout=15 ${FLEET_VPS_HOST:-lake-vps-lor-main} 'systemctl --user thaw agents-adhoc.slice' >/dev/null 2>&1; }
ssh -o ConnectTimeout=15 ${FLEET_VPS_HOST:-lake-vps-lor-main} 'systemctl --user freeze agents-adhoc.slice' >/dev/null 2>&1 && log "froze agents-adhoc.slice for the deploy window"
# Freeze cap (2026-09-26: failing-deploy reruns kept agents-adhoc frozen ~50 min and blocked every vps-run on lor-main):
# thaw automatically after LAKE_DEPLOY_FREEZE_MAX_S (default 45 min) even if this script is still retrying.
( sleep ${LAKE_DEPLOY_FREEZE_MAX_S:-2700}; thaw; echo "$(date +%H:%M:%S) [lake-merge #$PR] ALERT freeze cap hit: thawed agents-adhoc.slice while deploy still running" >> $F/events.log ) & FREEZE_CAP_PID=$!
trap 'kill $FREEZE_CAP_PID 2>/dev/null; thaw; release' EXIT
run=""; for i in $(seq 1 30); do run=$(gh run list -R $REPO --workflow deploy-lor-api.yml -L 8 --json databaseId,headSha --jq ".[]|select(.headSha==\"$msha\")|.databaseId" | head -1); [ -n "$run" ] && break; sleep 15; done
[ -z "$run" ] && { log "STOP no deploy-lor-api run for ${msha:0:9}"; exit 5; }
for i in $(seq 1 60); do [ "$(gh run view $run -R $REPO --json status --jq .status)" = completed ] && break; sleep 20; done
concl=$(gh run view $run -R $REPO --json conclusion --jq .conclusion); log "deploy-lor-api run $run conclusion=$concl"
for try in 1 2; do
  [ "$concl" = success ] && break
  log "deploy failed (host pressure gate?); rerun $try in 10 min"; sleep 600
  gh run rerun $run -R $REPO --failed >/dev/null 2>&1; sleep 30
  gh run watch $run -R $REPO --exit-status >/dev/null 2>&1; concl=$(gh run view $run -R $REPO --json conclusion --jq .conclusion); log "rerun $try conclusion=$concl"
done
lh=$(ssh -o ConnectTimeout=15 ${FLEET_VPS_HOST:-lake-vps-lor-main} 'cd ~/lake-of-rage && git rev-parse --short=9 HEAD' 2>/dev/null)
git -C ${FLEET_WT_ROOT:-$HOME/fleet/wt}/lake-of-rage-wt-fleetbase fetch -q origin 2>/dev/null
if ! git -C ${FLEET_WT_ROOT:-$HOME/fleet/wt}/lake-of-rage-wt-fleetbase merge-base --is-ancestor $msha $lh 2>/dev/null; then log "DEPLOY INCOMPLETE: lor-main at $lh does not contain ${msha:0:9}"; rm -f $F/lake_deploying; exit 6; fi
u=""; for i in $(seq 1 40); do u=$(ssh ${FLEET_VPS_HOST:-lake-vps-lor-main} "systemctl --user list-units --all --no-legend --plain 'lor-api-deploy-verify-${msha:0:12}-*' | awk '{print \$1}' | head -1"); [ -n "$u" ] && break; sleep 15; done
if [ -n "$u" ]; then
  for i in $(seq 1 60); do [ "$(ssh ${FLEET_VPS_HOST:-lake-vps-lor-main} "systemctl --user is-active $u")" = active ] || break; sleep 20; done
  log "lor-main verify $u: $(ssh ${FLEET_VPS_HOST:-lake-vps-lor-main} "journalctl --user -u $u --no-pager -o cat | grep -E 'ALERT|SMOKE|OK|PASS|rollback|ROLLBACK' | tail -2 | tr '\n' ' '")"
else log "no lor-api-deploy-verify unit seen for ${msha:0:12} (pipeline-only deploy?)"; fi
thaw; log "thawed agents-adhoc.slice"
probe=$(ssh ${FLEET_VPS_HOST:-lake-vps-lor-main} bash -s -- $MODE < $F/lor_probe.sh); echo "$probe" > $F/runs/lakeprobe-$PR.txt
while read -r l; do log "probe $l"; done <<< "$probe"
flags=$(python3 - "$probe" <<'PY'
import sys,re
out=[]
for l in sys.argv[1].splitlines():
    m=re.match(r"(card|chart) (\S+) cold=\[(\d+) ([\d.]+)\] warm=\[(\d+) ([\d.]+)\]",l)
    if m:
        kind,cid,c1,t1,c2,t2=m.groups()
        lim=0.6 if kind=="card" else 1.25
        if c1!="200" or c2!="200": out.append(f"{kind} {cid} non-200 {c1}/{c2}")
        elif float(t2)>lim: out.append(f"{kind} {cid} warm {t2}s > {lim}s")
    m=re.match(r"ready (\d+)/20",l)
    if m and m.group(1)!="20": out.append(f"ready {m.group(1)}/20")
print("; ".join(out))
PY
)
log "baseline flags: ${flags:-none}"
w=0; while :; do rs=$(ssh lake-vps "git -C ~/lake-of-rage rev-parse HEAD; git -C ~/lake-of-rage merge-base --is-ancestor $msha HEAD 2>/dev/null && echo contains || echo missing; systemctl --user show gold-replica-api -p ActiveEnterTimestamp --value"); rh=$(echo "$rs" | sed -n 1p); rc=$(echo "$rs" | sed -n 2p); rt=$(date -d "$(echo "$rs" | sed -n 3p)" +%s 2>/dev/null || echo 0)
  [ "$rc" = contains ] && [ "$rt" -gt "$t0" ] && break
  w=$((w+30)); [ $w -ge 300 ] && { log "replica not refreshed to ${msha:0:9} after 5 min (head ${rh:0:9}, restart $(echo "$rs"|sed -n 3p))"; break; }; sleep 30; done
rp=$(ssh lake-vps 'for i in 1 2 3; do sleep 1.6; curl -s -o /dev/null -w "%{http_code} %{time_total}  " --max-time 10 127.0.0.1:8802/api/v1/cards/base1-4; done; echo; sleep 1; curl -s -o /dev/null -w "ready %{http_code}" --max-time 10 127.0.0.1:8802/health/ready')
log "replica :8802 head=${rh:0:9} restarted=$(date -d @$rt +%H:%M:%S) base1-4 [$rp]"
if [ -z "$KEEP" ]; then echo $(( $(date +%s) + 300 )) > $F/hold_silph_until; rm -f $F/lake_deploying; log "silph lock released (+5 min hold)"; fi
log "DONE ${msha:0:9} flags=${flags:-none}"
echo "OK ${msha:0:9}"
