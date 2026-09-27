#!/usr/bin/env bash
# fleet_reconcile.sh — the fleet's self-healing loop (replaces requeue_failclosed.sh). Every 60s, for every fb lane
# (dq1d-* lanes belong to documents-1d's fbgate_1d and are skipped):
#   ORPHAN  gate in progress per its status file, but no fbgate process and no status write for 30 min -> re-gate
#           (the gate died: killed, OOM, network, or the script changed under it)
#   INFRA   escalation that is not a verdict: agent died / fail-closed / github unreachable / empty gh answer /
#           gate tests could not run (INFRA) / worktree creation failed -> re-gate
#   REWORK  a real but unfinished verdict: fix round pushed nothing / no-push / after one fix round /
#           untestable-unresolved / merged-tree regression -> ONE fresh full gate per PR head (new lenses + fix loop);
#           the gate must still approve, so the quality bar is unchanged
#   MERGED/CLOSED PR -> recorded in automerge.hold / skipped
# Caps: 3 automatic re-gates per PR head (1 of them REWORK), <= 6 launches per pass, and new gates start only while
# live fbgate lanes < GATE_WIP (file $F/gate_wip, default 35; env MAXGATES overrides) so in-flight gates finish first.
# Launch order (finish-first, owner 2026-09-26): lake-of-rage PRs, then silphcoanalytics, then agent-fleet; oldest PR
# first within a repo. One open-PR listing per repo per pass (not one gh call per lane).
# Every 10 min: close_issues.py --apply (issues that merged PRs declare fixed).
F=${FLEET_OPS_HOME:-$HOME/fleet/ops}
SEEN=$F/requeued_failclosed.txt; REWORKED=$F/reworked.txt; HOLD=$F/automerge.hold; touch $SEEN $REWORKED $HOLD
gate_wip(){ local w; w=${MAXGATES:-$(cat $F/gate_wip 2>/dev/null)}; [[ "$w" =~ ^[0-9]+$ ]] || w=35; echo $w; }
INFRA="NEEDS-ESCALATION (fail-closed|infra: github unreachable|gate refused: .* is '' \(\)|cannot create (gate|fix|recheck) worktree|agent [^ ]+ died \(exit=|gate tests could not run at head .*INFRA)"
REWORK="NEEDS-ESCALATION (\(no-push after|fix round pushed nothing|after one fix round|\(untestable-unresolved|merged-tree regression check failed)"
PROGRESS=" (start @|step0 |find: |verify: |judge|recheck|round [0-9]+|fix pushed|lens )"
ev(){ echo "$(date +%H:%M:%S) [reconcile] $*" >> $F/events.log; }
# Upload bandwidth (~6 Mbit/s WiFi) is the binding resource, not CPU: every gate streams 4+ agents. Launch only
# while the link is uncongested (github TCP connect < NET_MAX_CONNECT s), so new gates don't push running ones
# into network deaths.
net_ok(){ local t; t=$(curl -s -o /dev/null -m 5 -w '%{time_connect}' https://api.github.com 2>/dev/null); awk -v t="${t:-9}" -v m="${NET_MAX_CONNECT:-0.8}" 'BEGIN{exit !(t>0 && t<m)}'; }
# Remote gate worker (lor-main, ~/fleet/fb, OpenRouter agents in fleet.slice): lanes gating there are listed in
# $F/remote_live.txt by remote_sync.sh and count as live. New gates go remote first while fewer than
# $F/remote_gate_slots (default 10; 0 disables) run there, which frees the laptop uplink; local WIP applies otherwise.
remote_slots(){ local w; w=$(cat $F/remote_gate_slots 2>/dev/null); [[ "$w" =~ ^[0-9]+$ ]] || w=10; echo $w; }
gating(){ ps -eo args | awk '{for(i=1;i<=NF;i++) if($i ~ /(^|\/)fbgate$/){print $(i+1); break}}' | sort -u; }
# STUCK: a gate process is alive but its status file has not moved for STUCK_SECS (default 90 min). Seen 2026-09-26:
# 39 heavy gates sat in their lens stage for up to 3.5 h, holding agents and bandwidth without finishing. Kill the
# gate's process group and its agents (each agent runs under `timeout`, its own process group, found by the lane name
# in its prompt file on stdin), then mark it infra-escalated so the next pass re-gates it on the current gate.
kill_stuck(){ # kill_stuck LANE AGE_S
  local l=$1 age=$2 g a p f name pg
  for g in $(ps -eo pid=,pgid=,args= | awk -v l="$l" '$1==$2 && $0 ~ ("fbgate " l " ") {print $1}'); do
    a=$(ps -o args= -p $g); case "$a" in *fbgate\ $l\ *) kill -TERM -- -$g 2>/dev/null;; esac; done
  for p in $(ps -eo pid=,comm= | awk '$2=="command-code"{print $1}'); do
    f=$(readlink /proc/$p/fd/0 2>/dev/null) || continue
    name=$(basename "$f" | sed -E 's/^\.prompt\.//; s/\.[A-Za-z0-9]{4}(\.c)?$//')
    case "$name" in rev-gate-$l-*|rev-gate-j*-$l|gate-j*-$l|gatefix*-$l|gate-adj*-$l)
      pg=$(ps -o pgid= -p $p | tr -d ' '); [ "$(ps -o comm= -p $pg)" = timeout ] && kill -TERM -- -$pg 2>/dev/null;; esac
  done
  echo "$(date +%H:%M:%S) NEEDS-ESCALATION fail-closed: gate stuck ${age}s without progress; stopped by reconcile for re-gate" >> $F/lanes/$l.status
  ev "[$l] stuck gate stopped (no status change ${age}s); re-gate next pass"
}
# Adaptive agent cap (AIMD; owner: use space-bunny agents intelligently, balance speed vs blowing up the link).
# fbrun admits a new agent only while fewer than $F/agent_total_max command-code processes run. Each pass:
# >6 network deaths in the last 10 min or github connect >1.0 s -> cap -10 (floor 30); 0 deaths and connect <0.5 s -> +5 (ceiling 90).
tune_cap(){
  # Owner 2026-09-26: no agents on the laptop — all agents run on lor-main; $F/local_agents_off pins the local cap at 0.
  [ -e $F/local_agents_off ] && return 0
  local cur deaths t new since
  cur=$(cat $F/agent_total_max 2>/dev/null || echo 80)
  since=$(date -d '-10 min' +%H:%M:%S)
  deaths=$(tail -3000 $F/events.log | awk -v t=$since '$1>=t && /network death/' | wc -l)
  t=$(curl -s -o /dev/null -m 5 -w '%{time_connect}' https://api.github.com 2>/dev/null)
  if [ "$deaths" -gt 6 ] || awk -v t="${t:-9}" 'BEGIN{exit !(t==0 || t>1.0)}'; then new=$(( cur-10 < 30 ? 30 : cur-10 ))
  elif [ "$deaths" -eq 0 ] && awk -v t="${t:-9}" 'BEGIN{exit !(t>0 && t<0.5)}'; then new=$(( cur+5 > 90 ? 90 : cur+5 ))
  else new=$cur; fi
  if [ "$new" != "$cur" ]; then echo $new > $F/agent_total_max.tmp && mv $F/agent_total_max.tmp $F/agent_total_max
    ev "agent cap $cur -> $new (network deaths/10min=$deaths, github connect=${t:-timeout}s)"; fi
}
last_close=0
while :; do
  $F/wait_net.sh
  [ $(( $(date +%s) % 300 )) -lt 90 ] && tune_cap
  live=$(gating); nlive=$(echo "$live" | grep -c .); launched=0; now=$(date +%s); wip=$(gate_wip)
  rlive=$(cat $F/remote_live.txt 2>/dev/null); nrlive=$(echo "$rlive" | grep -c .); rlaunched=0; rslots=$(remote_slots)
  # 1) classify every lane locally (no gh calls): stuck gates are stopped, re-gate candidates collected
  cands=()
  for s in $F/lanes/*.status; do
    l=$(basename $s .status); case $l in dq1d-*|'') continue;; esac
    grep -qx "$l" $HOLD && continue
    grep -qx "$l" <<< "$rlive" && continue  # gating on lor-main
    if grep -qx "$l" <<< "$live"; then
      _age=$((now - $(stat -c %Y $s))); [ $_age -ge ${STUCK_SECS:-5400} ] && kill_stuck $l $_age
      continue
    fi
    pgrep -f "rebase_regate.sh $l " >/dev/null && continue
    last=$(grep -E "PREMERGE-APPROVED|NEEDS-ESCALATION|NEEDS-REBASE|$PROGRESS" $s | tail -1)
    kind=""
    if grep -qE "$INFRA" <<< "$last"; then kind=infra
    elif grep -qE "$REWORK" <<< "$last"; then kind=rework
    elif grep -qE "$PROGRESS" <<< "$last" && [ $((now - $(stat -c %Y $s))) -ge 1800 ]; then kind=orphan
    fi
    [ -n "$kind" ] && cands+=("$l $kind")
  done
  # 2) resolve candidates against ONE open-PR listing per repo, order lake > silph > fleet, oldest PR first
  if [ ${#cands[@]} -gt 0 ] && { [ $nlive -lt $wip ] || [ $nrlive -lt $rslots ]; }; then
    openprs=$(for r in lake-of-rage silphcoanalytics agent-fleet; do
      gh pr list -R ${FLEET_GH_OWNER:-Evan-Kim2028}/$r --state open --limit 300 --json number,headRefName,createdAt,headRefOid \
        --jq ".[] | select(.headRefName|startswith(\"fb/\")) | \"$r \(.number) \(.headRefName[3:]) \(.headRefOid[:9]) \(.createdAt)\"" 2>/dev/null
    done)
    ordered=$(printf '%s\n' "${cands[@]}" | awk -v open="$openprs" '
      BEGIN{ n=split(open, L, "\n"); rank["lake-of-rage"]=0; rank["silphcoanalytics"]=1; rank["agent-fleet"]=2
             for(i=1;i<=n;i++){ split(L[i], f, " "); if(f[3]!="") { repo[f[3]]=f[1]; pr[f[3]]=f[2]; head[f[3]]=f[4]; ts[f[3]]=f[5] } } }
      { if($1 in repo) print rank[repo[$1]], ts[$1], $1, $2, repo[$1], pr[$1], head[$1]; else print "9 - " $1 " " $2 " - - -" }' | sort -k1,1n -k2,2)
    while read -r _rk _ts l kind r pr head <&3; do
      [ -n "$l" ] || continue
      [ $launched -ge 6 ] && break
      _remote_ok=0; case "$r" in lake-of-rage|silphcoanalytics|agent-fleet) [ $((nrlive + rlaunched)) -lt $rslots ] && _remote_ok=1;; esac
      [ $_remote_ok = 0 ] && [ $((nlive + launched - rlaunched)) -ge $wip ] && break
      [ $_remote_ok = 0 ] && [ -e $F/local_agents_off ] && break  # remote full and laptop is agent-free: wait
      if [ "$_rk" = 9 ]; then  # no open fb/ PR: merged -> hold; closed/absent -> skip (re-checked at most every 30 min)
        mkdir -p $F/reconcile_nopr; _m=$F/reconcile_nopr/$l
        [ -e $_m ] && [ $((now - $(stat -c %Y $_m))) -lt 1800 ] && continue; touch $_m
        st=$(for rr in lake-of-rage silphcoanalytics agent-fleet; do gh pr list -R ${FLEET_GH_OWNER:-Evan-Kim2028}/$rr --head fb/$l --state merged --json number --jq 'length' 2>/dev/null; done | awk '{s+=$1} END{print s+0}')
        [ "$st" -gt 0 ] && echo "$l" >> $HOLD; continue
      fi
      net_ok || { ev "network congested; deferring re-gates this pass"; break; }
      n=$(grep -cx "$l $head" $SEEN); [ "$n" -ge 3 ] && continue
      if [ $kind = rework ]; then grep -qx "$l $head" $REWORKED && continue; echo "$l $head" >> $REWORKED; fi
      echo "$l $head" >> $SEEN; launched=$((launched+1))
      echo "$(date +%H:%M:%S) reconcile: re-gate ($kind) #$((n+1)) @$head" >> $F/lanes/$l.status
      ev "[$l] re-gate ($kind) #$((n+1)) @$head (PR $r#$pr, wip $((nlive + launched))/$wip)"
      if [ $_remote_ok = 1 ] && $F/fbgate_remote $l $r $pr >> $F/gate-$l.log 2>&1; then
        rlaunched=$((rlaunched+1)); ev "[$l] gate routed to lor-main (remote $((nrlive + rlaunched))/$rslots)"
      elif [ -e $F/local_agents_off ]; then
        # Owner 2026-09-26: no agents on the laptop. A refused/failed remote start (admission closed by the lor-main
        # pressure guard, unreachable host) is deferred to a later pass and does not use up a re-gate attempt.
        grep -vx "$l $head" $SEEN > $SEEN.tmp; mv $SEEN.tmp $SEEN; launched=$((launched-1))
        ev "[$l] remote start refused ($(tail -1 $F/gate-$l.log | cut -c1-80)); deferred, no local fallback"; break
      else
        setsid nohup $F/fbgate $l $r $pr > $F/gate-$l.log 2>&1 < /dev/null &
      fi
      sleep 2
    done 3<<< "$ordered"
  fi
  if [ $((now - last_close)) -ge 600 ]; then last_close=$now; timeout 900 python3 $F/close_issues.py --apply > $F/runs/.close_issues.log 2>&1; fi
  sleep 60
done
