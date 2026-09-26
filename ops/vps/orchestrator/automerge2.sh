#!/bin/bash
# automerge2: every tick, collect ALL approved lanes whose PR head == approved sha.
#   lake-of-rage: 2+ ready -> ONE lake_batch_merge (back-to-back merges, one deploy + one verify); 1 -> lake_merge_verify.
#   silphcoanalytics: serial (deploy workflow per push), one per tick.
# Stale approvals (head moved) are skipped. Merged lanes go to automerge.hold.
F=${FLEET_OPS_HOME:-$HOME/fleet/ops}
HOLD="$F/automerge.hold"; touch $HOLD
ev(){ echo "$(date +%H:%M:%S) [automerge2] $*" >> $F/events.log; }
while :; do
  lake=(); lakelanes=(); silph=()
  for s in $F/lanes/*.status; do
    l=$(basename $s .status); grep -qx "$l" $HOLD && continue
    grep -qx "$l" $F/merge_hold_clusterB.txt 2>/dev/null && continue  # cluster B merges wait for sales pass 2 downstream
    line=$(grep -E "PREMERGE-APPROVED|NEEDS-ESCALATION|NEEDS-REBASE|loop result|gate: start| start @" $s | tail -1)
    case "$line" in *PREMERGE-APPROVED*) sha=${line##* };; *"loop result: APPROVE"*) sha=${line##* };; *) continue;; esac
    [ ${#sha} -ge 7 ] || continue
    for repo in lake-of-rage silphcoanalytics; do
      pr=$(gh pr list -R ${FLEET_GH_OWNER:-Evan-Kim2028}/$repo --head fb/$l --state open --json number,headRefOid --jq ".[] | select(.headRefOid | startswith(\"$sha\")) | .number" 2>/dev/null)
      [ -n "$pr" ] || continue
      if [ $repo = lake-of-rage ]; then lake+=("$pr:${sha:0:9}"); lakelanes+=("$l"); else silph+=("$pr:${sha:0:9}:$l"); fi
    done
  done
  # Batch window (owner 2026-09-26: bigger, less frequent deploys): per repo, merge only when >= BATCH_MIN approved PRs
  # are ready or the oldest has waited BATCH_WAIT_S; then ONE merge batch + ONE deploy. First-seen times live in
  # $F/approved_seen/<lane> (mtime), cleared once the lane is merged.
  mkdir -p $F/approved_seen; now=$(date +%s)
  ready(){ # ready LANE... -> 0 if the batch should fire
    local n=$# oldest=$now l f
    for l in "$@"; do f=$F/approved_seen/$l; [ -e $f ] || touch $f; [ $(stat -c %Y $f) -lt $oldest ] && oldest=$(stat -c %Y $f); done
    [ $n -ge ${BATCH_MIN:-5} ] || [ $((now - oldest)) -ge ${BATCH_WAIT_S:-1800} ]; }
  silphlanes=(); for it in "${silph[@]}"; do r=${it#*:}; silphlanes+=("${r#*:}"); done
  if [ ${#lake[@]} -gt 0 ] && ! ready "${lakelanes[@]}"; then lake=(); fi
  if [ ${#silph[@]} -gt 0 ] && ! ready "${silphlanes[@]}"; then silph=(); fi
  for l in $(ls $F/approved_seen 2>/dev/null); do grep -qx "$l" $HOLD && rm -f $F/approved_seen/$l; done
  if [ ${#lake[@]} -gt 0 ]; then
    ( flock -n 9 || exit 0
      if [ ${#lake[@]} -ge 2 ]; then ev "lake BATCH ${lake[*]} (${lakelanes[*]})"; $F/lake_batch_merge.sh "${lake[@]}" > $F/runs/.am2-lakebatch.log 2>&1
      else ev "lake single ${lake[0]} (${lakelanes[0]})"; $F/lake_merge_verify.sh ${lake[0]%%:*} ${lake[0]##*:} > $F/runs/.am2-lake.log 2>&1; fi
      rc=$?; ev "lake merge rc=$rc (${lakelanes[*]})"
      for i in "${!lake[@]}"; do read -r st mg < <(gh pr view ${lake[$i]%%:*} -R ${FLEET_GH_OWNER:-Evan-Kim2028}/lake-of-rage --json state,mergeable --jq '"\(.state) \(.mergeable)"')
        [ "$st" = MERGED ] && echo "${lakelanes[$i]}" >> $HOLD
        if [ "$st" = OPEN ] && [ "$mg" = CONFLICTING ]; then echo "$(date +%H:%M:%S) NEEDS-REBASE conflicting with main; rebase_regate launched" >> $F/lanes/${lakelanes[$i]}.status
          [ -e $F/local_agents_off ] && ev "rebase needed but laptop is agent-free: left for lor-main/orchestrator" || setsid nohup $F/rebase_regate.sh ${lakelanes[$i]} lake-of-rage ${lake[$i]%%:*} > $F/runs/.rebase-${lakelanes[$i]}.log 2>&1 & fi
      done
    ) 9>$F/automerge.lake-of-rage.lock &
  fi
  if [ ${#silph[@]} -ge 2 ]; then
    # 2+ approved silph PRs: merge back-to-back and deploy/verify once (silph_batch_merge skips stale/conflicting ones).
    pairs=(); lanes=(); for it in "${silph[@]}"; do p=${it%%:*}; r=${it#*:}; pairs+=("$p:${r%%:*}"); lanes+=("${r#*:}"); done
    ( flock -n 9 || exit 0
      ev "silph BATCH ${pairs[*]} (${lanes[*]})"; FORCE_MERGE=1 $F/silph_batch_merge.sh "${pairs[@]}" > $F/runs/.am2-silphbatch.log 2>&1; rc=$?
      ev "silph BATCH rc=$rc (${lanes[*]})"
      for i in "${!pairs[@]}"; do read -r st mg < <(gh pr view ${pairs[$i]%%:*} -R ${FLEET_GH_OWNER:-Evan-Kim2028}/silphcoanalytics --json state,mergeable --jq '"\(.state) \(.mergeable)"')
        [ "$st" = MERGED ] && echo "${lanes[$i]}" >> $HOLD
        if [ "$st" = OPEN ] && [ "$mg" = CONFLICTING ]; then echo "$(date +%H:%M:%S) NEEDS-REBASE conflicting with main; rebase_regate launched" >> $F/lanes/${lanes[$i]}.status
          [ -e $F/local_agents_off ] && ev "rebase needed but laptop is agent-free: left for lor-main/orchestrator" || setsid nohup $F/rebase_regate.sh ${lanes[$i]} silphcoanalytics ${pairs[$i]%%:*} > $F/runs/.rebase-${lanes[$i]}.log 2>&1 & fi
      done
    ) 9>$F/automerge.silphcoanalytics.lock &
  elif [ ${#silph[@]} -gt 0 ]; then
    item=${silph[0]}; pr=${item%%:*}; rest=${item#*:}; sha=${rest%%:*}; l=${rest#*:}
    ( flock -n 9 || exit 0
      ev "silph #$pr ($l @$sha)"; FORCE_MERGE=1 $F/silph_merge_verify.sh $pr $sha > $F/runs/.am2-$l.log 2>&1; rc=$?
      ev "silph #$pr ($l) merge+verify rc=$rc"; [ $rc = 0 ] && echo "$l" >> $HOLD
      if [ $rc = 3 ]; then echo "$(date +%H:%M:%S) NEEDS-REBASE conflicting with main; rebase_regate launched" >> $F/lanes/$l.status
        [ -e $F/local_agents_off ] && ev "rebase needed but laptop is agent-free: left for lor-main/orchestrator" || setsid nohup $F/rebase_regate.sh $l silphcoanalytics $pr > $F/runs/.rebase-$l.log 2>&1 & fi
    ) 9>$F/automerge.silphcoanalytics.lock &
  fi
  sleep 180
done
