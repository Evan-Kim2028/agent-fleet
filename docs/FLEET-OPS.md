# Fleet Ops — the multi-operator lane manager

`agent-fleet lane run` drives one coding lane end to end, for **two concurrent
operator sessions** working the same repos. It replaces the bash fleet drivers
(`xlane`, `fbrun`, `devin_finish.sh`, `fbstatus`, `automerge.sh`) with a manager
that guarantees the one thing those drivers kept failing to do: **a PR always
exists when the lane finishes.**

```
agent-fleet lane run --operator documents-1d --lane movers \
    --repo-path ~/Documents/silphcoanalytics \
    --task-file dq/lanes/movers.md \
    --status-file ~/fb/lanes/movers.status

agent-fleet lanes status --all
agent-fleet lanes stop movers --operator documents-1d
```

---

## What a lane does, in order

The order is the design. Each step exists because skipping it is how a lane was
lost in the bash era.

1. **Worktree** — created or *reused* on the lane's branch. An interrupted lane
   usually has real work in its worktree; creating a fresh one abandons it.
2. **Register** — the lane's `(pid, pgid, starttime)` is recorded *before* the
   engine spawns, so a concurrent `lanes stop` always has something valid.
3. **Engine** — the implementer runs under a memory cap with the standing fences
   in its prompt. A lazy exit is recorded but is not fatal.
4. **PR guarantee** — runs *even when the engine failed*. Leftover changes are
   committed (hooks live), commits are pushed, and a PR is opened if none exists.
5. **Binding** — the repo is derived from the worktree's own `origin`, and the
   PR's `headRefName` must be this lane's branch. A mismatch is a hard refusal.
6. **Gate** — feature-detected; skipped cleanly when the gate pipeline is not
   installed.
7. **Status line** — `HH:MM:SS PREMERGE-APPROVED <sha9>` or
   `HH:MM:SS NEEDS-ESCALATION <reason>`, appended to `--status-file`.
8. **Hooks** — the operator's `on_approved` or `on_escalated` command.

### The PR guarantee

This is the whole point. The recurring failure was: the implementer finished,
the branch had real commits, and *no PR existed* — so the gate, the review loop
and the automerge had nothing to act on and the work sat forever. The old
drivers wrote `NEEDS-ATTENTION no PR` and waited for a human to go commit, push
and open a PR by hand.

`lane run` closes that loop. If the agent died with work on the branch, that is
precisely the case that most needs the guarantee, so it runs anyway.

**On hooks.** The manager commits with plain `git commit` so the repo's real
hooks run. `--no-verify` is never used. The only concession is a `SKIP=`
environment naming the hook ids the repo declared as baseline-red in
`fleet_ops.baseline_skip_hooks` — pre-commit's own selective-skip mechanism, and
narrower than disabling hooks. If a *non*-baseline hook fails, the commit fails
and the lane escalates with the hook output attached, which is correct: that is
a real problem with the diff, not something to bypass.

### The binding (why a lane cannot judge the wrong PR)

A stray `REVIEW_REPO` env var once sent lake-of-rage PR #3544 to
silphcoanalytics PR #3544, and four review lenses started on the wrong code.

Two facts must hold before anything reviews, and both are derived from the
lane's own worktree rather than from anything inherited:

- **The repo comes from the worktree's `origin` remote.** A lane in
  `~/Documents/silphcoanalytics-wt-fb-foo` is a silphcoanalytics lane, full
  stop. The lane name, the main working tree, and the environment are untrusted.
- **The PR's `headRefName` must be the lane's branch.** Following a PR whose
  head is a different branch would let the gate approve or fix someone else's
  work.

`--expected-repo OWNER/REPO` adds an operator-side assertion. A mismatch refuses
rather than "correcting" itself, because the operator knows which session they
are in.

---

## Configuration

Everything is an additive `fleet_ops:` block in the repo's `.agent-fleet.yaml`.
A repo without it is unaffected.

```yaml
fleet_ops:
  base_branch: main
  stall_minutes: 20
  # Hook ids the manager's auto-commit may pass via SKIP=. Every other hook runs.
  baseline_skip_hooks: [ruff-format, pyright]
  # Appended to the house fences; can add rules, never shorten them.
  fences:
    - "silph: api/lor_client.py is fenced by the orchestrator; do not edit."
  operators:
    documents-0e:
      engine: cmd                 # cmd is pinned to stealth/space-bunny-alpha
      push_branch: fb/{lane}
      task_file: prompts/{lane}.task.md
      on_approved: "cp $STATUS $REPO/reviews/$PR-$SHA9.md"

    documents-1d:
      engine: cmd                 # space-bunny for everything, judge included
      push_branch: dq1d/{lane}    # follow the existing PR's head
      task_file: dq/lanes/{lane}.md
      judge_engine: cmd           # never grok for this operator
      on_approved: "printf 'VERDICT: APPROVE' > dq/reviews/$PR-$SHA9.md"
      on_escalated: "echo \"VERDICT: ESCALATE exit=$exit\" >> dq/reviews/$PR.gate.bg.log"
```

`{lane}` and `{operator}` are expanded in template values. `on_escalated` fires
for a stall, a lazy exit, a failed commit, a rejected gate, or a refused binding —
anything that is not an approval.

### Dispatch and admission

Two more optional blocks, used by [`fleet dispatch`](#dispatching-a-queue) and by
every lane's test runs. Both default, so a repo that omits them is unaffected.

```yaml
fleet_ops:
  dispatch:
    max_lanes: 8                # concurrent `fleet lane run` children
    max_gates: 4                # concurrent gates. Deliberately low: 18 at once
                                # is what drove the box to load 200.
    psi_avg10_max: 25.0         # CPU pressure ceiling, in percent
    psi_path: /sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/agents.slice/cpu.pressure
    cluster_order: [C0, C1, C2] # launch order; unknown clusters sort last
  admission:
    tests: 12                   # shared pytest slots (both operators)
    typecheck: 4                # shared pyright / pre-commit slots
    shared_dir: null            # null -> ~/.agent-fleet/admission
    nice: 5                     # admitted runs yield to a committing lane
```

`shared_dir` is what makes the pools *shared*: two operators pointing at the
same directory contend for the same slots, which is the point, because the
constraint is the hardware rather than the repo.

### Hook environment

Hooks run through a shell with a **small, explicit environment** — not the
manager's. A hook is operator-authored config, and handing it the whole manager
environment would leak credentials and internal paths into a command nobody
wrote here.

| Variable | Meaning |
| --- | --- |
| `LANE`, `OPERATOR` | lane name, operator name |
| `REPO` | repo path |
| `PR` | PR number |
| `SHA9` | 9-char head sha |
| `STATUS` | the status file path |
| `VERDICT` | `approve` or `escalate` |
| `exit`, `RC` | exit code, when the lane escalated |

A hook that fails is reported, never fatal: the lane's verdict is already
recorded, and a broken downstream notifier must not rewrite it.

---

## Model policy

Narrow and enforced in code, not left to operator discipline:

| Engine | Model | Role |
| --- | --- | --- |
| `cmd` | `stealth/space-bunny-alpha` | implement |
| `devin` | `swe-2-high` → `swe-2-medium` | implement (ladder on capacity error) |
| `grok` | `step-5-preview` | **judge only** |

An out-of-policy model raises **before any subprocess is spawned**, so a stray
`AGENT_FLEET_MODEL` or `FB_MODEL` in the environment cannot redirect a lane.
The bash drivers enforced this only by `unset FB_MODEL` plus a hardcoded `-m`.

`grok` is rejected outright for `role="implement"`. An operator that wants cmd
for the judge as well sets `judge_engine: cmd`.

---

## Engine behaviour

**cmd** (`--max-turns 900` by default). Exit 8 means the *turn cap* stopped the
run, not the work: the manager resumes it once with a continue prompt. The JSONL
stream is judged after the fact for a **lazy exit** — a zero exit with no tool
calls, or with few tool calls plus a refusal. Both read as success to a naive
caller; exit 86 marks them as failures, preserving the old driver muscle memory.

**devin**. A capacity error walks *down* the model ladder, because the high tier
is frequently out of capacity while the medium tier is not. A run truncated by
the output-token ceiling with no PR yet gets **one** continue — the bash driver
looped up to ten times, which could burn hours re-reading the same context.

Both engines are launched memory-capped (`systemd-run --user --scope`, falling
back to `ulimit -v`) with `MemorySwapMax=0`, and detached with
`start_new_session=True` so a dying tool shell does not take the lane with it.

### Stall and lazy-exit handling

A lane that stops making progress still looks alive: the process is running, so
`lanes status` says `running`, and nothing distinguishes "thinking hard" from
"wedged". Stall is measured from *tool activity* — the JSONL stream's mtime — not
process liveness.

The policy is deliberately conservative: **one** automatic continue, then
escalate. A single continue clears the common causes. A second stall is not
transient, and escalating surfaces it instead of silently spending hours.

---

## `lanes status`

```
$ agent-fleet lanes status --all
lanes (all operators)
LANE       OPERATOR        REPO                    STATE          PHASE  PR     HEAD      AGE   IDLE-FOR  TOOL-ERR%  LAST-STATUS
----------  --------------  ----------------------  -------------  -----  ------  --------  ----  --------  ---------  --------------------------------
movers     documents-1d    Evan-Kim2028/silphco…   running        impl   #3544  abcdef123  12m   45s       3%         12:04:31 PREMERGE-APPROVED abcdef123
mv         documents-0e    Evan-Kim2028/lake-of…   approved       done   -      9f2c1a04  2h10m  2h10m     0%         -
```

- `--operator X` narrows to one session; `--all` (the default) spans every
  operator, which is the point of a shared registry.
- A record left in `running` whose process is gone is shown as **stalled**.
  Reporting it as running is how a lane got lost: the file said running forever
  because nothing reconciled it with reality.
- `--json` gives the same rows for scripting.

State lives in `~/.agent-fleet/lanes/<operator>/<lane>.json` plus a shared
append-only `events.jsonl`. Splitting by operator matters because two sessions
work the same repos concurrently and neither should clobber the other's record.

---

## `lanes stop`

```bash
agent-fleet lanes stop movers --operator documents-1d
```

Kills **exactly one lane's process group**, by the `(pid, pgid, starttime)`
recorded at launch. It never matches on a command line.

The bash drivers used `pkill -f` and `pgrep -x devin` + a cwd comparison. Both
are wrong here: `pkill -f` matches any process whose command line merely
*contains* the pattern, so a sibling operator's lane — same binary, same worktree
layout, different lane — dies with the intended one.

The manager instead refuses, rather than guessing, in four cases:

| Refusal | Why |
| --- | --- |
| `refused_own_pgid` | the recorded pgid is the caller's own group — never signal it |
| `refused_pid_reused` | the pid's start-time fingerprint changed; the number was recycled |
| `refused_ambiguous_lane` | two operators own this lane name — pass `--operator` |
| `refused_lane_not_found` | no such lane |

A lane that is already gone reports `already_gone` as success, not failure.

---

## Migration from the bash drivers

| Bash driver | What it did | Replacement |
| --- | --- | --- |
| `xlane` | impl (devin/grok/cmd) → premerge → fix loop | `agent-fleet lane run` — same order, plus the PR guarantee and the binding check |
| `fbrun` | headless `cmd`, lazy-exit detection, final-text extraction | `agent_fleet.fleet_ops.engines.run_cmd_engine` + `lazyexit` — same thresholds, same exit 86 |
| `devin_finish.sh` | devin capacity fallback + max-output-token continue | `run_devin_engine` — same ladder, one continue instead of ten |
| `fbstatus` | per-run status table | `agent-fleet lanes status` — keyed on lanes, unified across operators |
| `automerge.sh` | tails lane status files for `PREMERGE-APPROVED` | unchanged contract: the same status file, same token, same sha9 |
| `pkill -f` / `pgrep -x devin` | kill a lane by pattern | `agent-fleet lanes stop` — by recorded process group |
| `prompts/fences.md` | standing fences appended to every prompt | `agent_fleet.fleet_ops.fences` — in code, so they cannot be cleaned away |

**Nothing downstream has to change.** The status-file format, the
`PREMERGE-APPROVED <sha9>` token and the `NEEDS-ESCALATION <reason>` line are
byte-compatible, so `automerge.sh` and both operators' monitors keep working
against a lane that is now managed.

A sha that is not a real short sha is never written as an approval: the old
automerge took the last field of the line and would otherwise try to merge a PR
whose head started with that word, find none, and loop.

---

## Dispatching a queue

`fleet dispatch QUEUE.jsonl --operator NAME` runs a whole triaged queue: it
launches `fleet lane run --no-gate` per item, and gates whatever produced a PR.

```bash
fleet dispatch triage.jsonl \
    --operator documents-0e \
    --repo lake-of-rage=/home/evan/Documents/lake-of-rage-wt-fleetbase \
    --repo silphcoanalytics=/home/evan/Documents/silphcoanalytics-wt-fleetbase \
    --max-lanes 8 --max-gates 4
```

Each queue line is a JSON object. Only `lane`, `repo` and `task` are required;
the rest is triage metadata that is carried into the generated task file and
into scheduling:

```json
{"lane": "gold-catalog-batch", "repo": "lake-of-rage", "ref": "R-4821",
 "cluster": "C0", "depends_on": ["R-4800"], "area": "gold", "size": "M",
 "task": "...", "evidence": "...", "files": ["pipe/gold/build.py"],
 "dbt_models": ["gold.listings"]}
```

`depends_on` names another item's **`ref`** (or its lane name) and is released
when that lane reaches a terminal state — *approved or escalated*. A dependency
exists to serialise lanes that touch the same files; a lane that failed must not
take the rest of its chain hostage with it.

### The state machine and the restart contract

Each lane moves `queued → running → pr → gating → done`, or to `failed` if it
could not be started at all. That is durable per operator:

```
~/.agent-fleet/lanes/dispatch/<operator>/state.json
```

The restart contract is the important part:

- a lane is **never relaunched** once it has a terminal state, and
- a lane with a **live recorded process** is re-attached to, not relaunched.

Liveness is a recorded `(pid, starttime)` fingerprint, not a command-line match.
That matters twice over: a recycled pid cannot be mistaken for the original
lane, and two operators cannot adopt each other's work.

Because the state is durable, a dispatcher that dies mid-queue can simply be
run again with the same command line. It picks up where it left off.

### Throttling: CPU pressure, not load average

The shell dispatcher this replaces throttled on `os.getloadavg()`, and that was
wrong for this machine. The agents run inside a cgroup with a **CPU quota**, so
a quota-throttled task is still counted as *running* by the load average: load
reads high while the machine is idle-but-stalled, and the dispatcher sat on its
hands for forty minutes refusing to launch anything.

The throttle is CPU **PSI** — `some avg10` from the agents slice's
`cpu.pressure`, which is the percentage of the last ten seconds in which at least
one task was runnable but had to wait. `full` is never used (throttling on it is
far too strict) and PSI is read **fail-open**: an unreadable file never blocks a
launch, because the incident this replaces was a false block.

### `--gate-cmd`

With no `--gate-cmd`, a lane that produced a PR goes to the built-in
`agent-fleet gate`. A template replaces that:

```bash
--gate-cmd '/opt/fbgate {lane} {repo} {pr}'
```

Placeholders are `{lane}`, `{pr}`, `{repo}`, `{slug}` and `{operator}`. The
expanded string is split with `shlex` and executed **without a shell**, so a `;`
or `&&` in a template stays inert data rather than becoming a second command.

### What this makes impossible

Each of these was a real failure of the shell dispatcher, and each has a test
named after it in `tests/test_fleet_ops_dispatch.py`:

| Failure | What prevents it |
| --- | --- |
| a finished lane missing from the queue crashed the dispatcher (`StopIteration`), so finished lanes were never gated | the queue is a dict; an unresolvable lane is *recorded* as `unknown_item`, never looked up |
| two operators shared one log and adopted each other's lanes | state is namespaced by operator; events are tagged, and liveness is a pid fingerprint |
| restarting relaunched lanes that had already run | terminal state and live pids both suppress a launch |
| eighteen gates released at once (load 200) | `--max-gates` is counted *within* the tick, so the cap is exact |
| concurrent gates' `prune` deleted a sibling's half-created worktree | worktree add/remove/prune are serialized per repository (see below) |
| `loadavg` throttling blocked every launch for 40 minutes | CPU PSI, read fail-open; `getloadavg` is never called |

A dependency **cycle** is the remaining way a queue can fail to drain, so it is
detected and the affected lanes are finished as `dependency_deadlock` rather than
waited on forever.

---

## Admission pools

Every lane runs a coding agent, and those agents run `uv run pytest` freely. Left
alone, twenty lanes each start a test suite the moment they are told to.

`fleet lane run` therefore writes a generated `uv` **shim directory** and puts it
first on the *engine* child's `PATH`. A matching invocation then waits for one of
N shared flock slots:

- `uv run … pytest` → the test pool (default 12)
- `uv run … pyright` / `pre-commit` → the typecheck pool (default 4)
- anything else (`uv sync`, `uv --version`, `uv run python -c 1`) → **passes
  straight through and takes no slot**

Three details are load-bearing:

- The shim resolves the *real* `uv` with its own directory stripped from `PATH`
  first, so it can never resolve to itself.
- The slot fd is made inheritable before `execv`. Python opens files
  `O_CLOEXEC` by default, so without that the `flock` is released the instant the
  real `uv` starts — and the pool silently admits everybody.
- The shim `exec`s rather than forks, so there is no window in which the slot is
  held but the real `uv` is not yet running.

Admission applies to the **engine only**. The gate and the per-operator hooks
keep the manager's environment: a gate that quietly queued behind a lane's test
slots would stall the merge path.

Slots live under `~/.agent-fleet/admission/slots/<pool>/slot.<i>`, so they are
**shared across operators** by default. They are flock-based, which means the
kernel releases them however a holder exits, including `SIGKILL` — a crashed
lane cannot leak a slot.

---

## The gate worktree lock

`agent_fleet/gate/gitops.py` serializes worktree `add`, `remove` and `prune`
per repository, under
`~/.agent-fleet/admission/locks/worktree-<slug>.lock`.

`git worktree add` registers a directory under `.git/worktrees` and only then
populates it. A concurrent `prune` — which is what removing an already-gone
worktree does — deletes that registration, and the in-flight `add` dies with:

```
fatal: could not open '.git/worktrees/<name>/locked' for writing: No such file or directory
```

Reproduced at roughly one failure per 40 single-shot adds against concurrent
pruners, and it loses a sibling gate's worktree outright. The lock covers all
three operations *together*: locking only `add` would still let one gate's prune
run while another's add is mid-flight, which is the actual corruption.

The lock is keyed on the repo's `origin` slug, so two checkouts of the same
repository share one lock. It is re-entrant per thread (`prepare_worktree` calls
`remove_worktree`), and it **degrades to running unlocked** if the lock file
cannot be created — a gate that cannot take a lock is still better than a gate
that refuses to run.

Every call site in `gate/pipeline.py` goes through `prepare_worktree` /
`remove_worktree`, so it inherits the lock with no change there.

---

## Module map

| Module | Responsibility |
| --- | --- |
| `config.py` | the `fleet_ops:` block — per-operator engine, push target, hooks |
| `models.py` | the model policy, enforced before any spawn |
| `worktree.py` | create or reuse a lane's isolated worktree; never destroys |
| `engines.py` | cmd and devin invocation: memory cap, fences, ladders, resume |
| `lazyexit.py` | the fbrun lazy-exit heuristic, ported |
| `stall.py` | stall detection; one continue, then escalate |
| `guarantee.py` | **the PR guarantee** — commit, push, open |
| `binding.py` | repo/PR binding; refuses the wrong-PR case |
| `gate.py` | the feature-detected gate seam |
| `registry.py` | per-operator lane state and the shared event stream |
| `status.py` | the `lanes status` table |
| `statusfile.py` | status lines and the per-operator hooks |
| `stop.py` | stop one lane by recorded process group |
| `memcap.py` | the memory cap and targeted-test scoping |
| `fences.py` | the standing fences carried into every prompt |
| `runner.py` | the orchestration, in the order above |
| `cli.py` | `lane run`, `lanes status`, `lanes stop`, `dispatch QUEUE.jsonl` |
| `dispatch.py` | durable queue dispatch: scheduling, state, restart safety |
| `pressure.py` | CPU PSI, the throttle that replaced load average |
| `admission.py` | the generated `uv` shim and the shared slot pools |

---

## The gate seam

The `gate` pipeline (find → verify-with-a-failing-test → one fix → recheck) is
built in a parallel lane. This integrates through its entry point
(`agent-fleet gate …`) and **skips cleanly when it is not installed**:

- a lane that cannot be gated still ends at `pr_guaranteed` with an explicit
  reason, rather than failing;
- detection is by capability (does the subcommand exist?), not by version, so
  the same call site starts working when the gate merges.

Nothing runs until the binding is verified. The gate is invoked with the
*verified* slug and head, and a gate that exists but fails is an **error**, not
a silent skip — at that point the feature is merged and a broken gate is worth
escalating.

Approval is read from the gate's own status line
(`PREMERGE-APPROVED <sha9>`) — the same contract the bash automerge consumed, so
a gate written to it needs no adapter. The verdict is read from the gate's
**last** line, because scanning the whole transcript would read
`NEEDS-ESCALATION: did not APPROVE the fix` as an approval.
