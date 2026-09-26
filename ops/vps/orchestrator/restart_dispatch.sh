#!/bin/bash
# restart_dispatch.sh QUEUE.jsonl [dispatch args...] — restart a dispatcher safely after a code change:
# pause it, adopt its running lanes and finished-but-ungated lanes, rewrite the queue to unlaunched items, relaunch.
F=${FLEET_OPS_HOME:-$HOME/fleet/ops}
Q=$1; shift; base=$(basename $Q .jsonl)
P=$(pgrep -f "^python3 dispatch.py $Q( |$)" | head -1); [ -n "$P" ] && kill -STOP $P
python3 - "$Q" "$base" "$F" <<'PY'
import json,re,subprocess,os,sys
F=sys.argv[3]
q,base=sys.argv[1],sys.argv[2]
items=[json.loads(l) for l in open(os.path.join(F,q)) if l.strip()]
ev=open(F+"/events.log").read(); ps=subprocess.run(["ps","-eo","args"],capture_output=True,text=True).stdout
launched=set(re.findall(r"\[dispatch\] launched (\S+)",ev))
running=[i['lane'] for i in items if f"--lane {i['lane']} " in ps]
ungated=[]
for i in items:
    l=i['lane']
    if l in running or l not in launched: continue
    fl=f"{F}/fleetlane/{l}.status"
    if not os.path.exists(fl): continue
    last=open(fl).read().strip().splitlines()[-1]
    if "guaranteed" not in last and "GATE-SKIPPED" not in last: continue
    g=f"{F}/lanes/{l}.status"
    if os.path.exists(g) and re.search(r" start @|PREMERGE|NEEDS",open(g).read()): continue
    if re.search(rf"fbgate {re.escape(l)} ",ps): continue
    ungated.append(l)
left=[i for i in items if i['lane'] not in launched]
open(f"{F}/triage/{base}.next.jsonl","w").write("".join(json.dumps(i)+"\n" for i in left))
json.dump(running+ungated,open(f"{F}/triage/adopt-{base}.json","w"))
print(f"{base}: running {len(running)} ungated {len(ungated)} left {len(left)}")
PY
[ -n "$P" ] && { kill -TERM $P; kill -CONT $P; }
mv $F/triage/$base.next.jsonl $F/triage/$base.jsonl
cd $F && setsid nohup python3 dispatch.py triage/$base.jsonl "$@" --adopt triage/adopt-$base.json > dispatch-$base.out 2>&1 < /dev/null &
echo "$(date +%T) [dispatch] restarted $base with adoption ($*)" >> $F/events.log
