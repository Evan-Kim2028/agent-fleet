# `fleet serve` — one supervisor for the whole pipeline

`fleet serve --operator NAME --config fleet.yaml` is a single long-running,
restart-safe process that keeps the fleet moving at maximum throughput inside
its resource limits, with no operator watching it.

It exists to replace the thing that made the bash fleet unmanageable. Those
scripts worked, and they required a human to be present: someone had to notice
a lane had stopped, notice a merge had wedged on a lock, notice an
`automerge.hold` had grown, and restart the pieces. Every one of those "notices"
is a rule in the watchdog here.

```
fleet serve run --operator documents-0e        # the long-running supervisor
fleet serve status --operator documents-0e      # one screen
fleet serve stop --operator documents-0e       # stop it
fleet serve watchdog --operator documents-0e   # what would it do? (dry run)
fleet serve decisions --operator documents-0e # the human queue
fleet serve capacity --operator documents-0e   # the file dispatch/gate read
```

## What it does and does not do

Serve **supervises**; it does not reimplement the pipeline. The dispatcher, the
merge executor and the janitor are separate programs spawned from configurable
command templates, so serve works with whatever those components happen to be —
the in-repo `fleet dispatch` and `fleet merge run`, or the bash drivers they
replace. The components keep their own logic instead of having it forked into a
supervisor that would then have to be kept in sync with them.

Serve owns everything *around* them:

| Concern | Module |
|---|---|
| restart, backoff, crash-loop detection, re-attach | `supervisor.py` |
| AIMD sizing from cgroup pressure | `capacity.py`, `pressure.py` |
| five self-healing rules | `watchdog.py`, `locks.py` |
| stage board, depth, throughput | `items.py` |
| reason-class routing of escalations | `escalate.py` |
| one screen, honest about what it could not measure | `status.py` |

## The safety rule everything else serves

**Remediations only ever act on what the fleet owns.** Every termination goes
through an exact pid plus a `/proc/<pid>/stat` field-22 start-time fingerprint
recorded when serve spawned the process. There is no name matching, no pattern
matching, and no scanning of processes serve did not start.

This is not a stylistic preference. The machine runs dozens of other agents,
several running the same engines with lane names in their argv — so the legacy
`watchdog.sh` approach of scanning `ps` for an engine name is a coin flip with
someone else's work on it. Two consequences worth knowing:

- **A pid alone is not an identity.** Linux recycles pids, so a recorded pid may
  name a completely different process. Nothing is signalled unless the live
  process still carries the recorded fingerprint, and a pid with no fingerprint
  is never signalled at all.
- **A zombie is not alive.** An unreaped child sits in `/proc` looking exactly
  like a live process. Liveness reads the process state field, so a supervisor
  never reports a corpse as running and never logs a "successful termination"
  of a process that exited on its own.

Run `fleet serve watchdog` (a dry run by default) to see every pid the watchdog
*would* signal and why, before trusting it to act.

## Supervision

Each component runs as a child with `start_new_session=True`, so it is its own
process-group leader and group termination cannot reach anything serve did not
spawn.

- **Restart on exit** with exponential backoff from `backoff_initial_s` to
  `backoff_max_s`, computed from the restart *count* rather than accumulated —
  so a supervisor that restarts mid-schedule resumes the same schedule instead
  of snapping back to zero and hammering a component that is failing
  immediately.
- **Crash-loop detection** over an N-crashes-in-M-minutes budget. Every exit
  carries a `cause`, and only `crash` counts toward the budget: a component the
  watchdog restarts for making no progress is not crash-looping, and conflating
  the two would eventually stop restarting it and hide the real fault behind a
  crash-loop alert. The crash history is persisted, so restarting serve does
  not hand a crash-looping component a fresh budget.
- **A crash-looping component does not take the fleet down.** It is marked
  `crash_looping`, an error event is emitted, and the other components keep
  running.

### Re-attach, never double-start

On boot, serve reads each component's pid file and requires the fingerprint to
match before adopting. A pid file is a cache of the truth, never the truth
itself. `start()` checks adoption first and unconditionally, so calling it
twice can never produce two processes for one role.

The supervisor holds an `flock` for its lifetime, taken *before* anything is
spawned. A second `fleet serve` for the same operator exits `3` with a message
naming the pid that holds it, rather than racing into a double dispatch.

## Capacity control

Every tick reads CPU/memory/IO pressure-stall information and the memory ratio
from one cgroup, and publishes targets to a file the components read.

**Never the load average.** `os.getloadavg()` is the obvious thing to reach for
and it is wrong here for a measurable reason: under a cgroup CPU quota the run
queue reflects tasks *throttled* by the quota, not work waiting for a CPU. A
4-way quota on a 16-core box reports a load of 64 while every core is 96% idle,
and admitting more work in response makes it worse. PSI measures the stall
directly. A test asserts this over the AST, so `from os import getloadavg` and a
literal `/proc/loadavg` read both fail it.

The rule is AIMD: additive increase below the low watermark, multiplicative
decrease above the high watermark, nothing in between. That middle band is the
hysteresis, and without it a controller oscillating across one threshold burns
half its decisions flipping direction. Floors and ceilings are hard limits from
config, applied *after* every adjustment rather than by clamping the input.

### A missing cgroup is not an idle machine

This is the failure mode that makes a capacity controller dangerous, so it gets
a first-class type rather than a float. `PressureReading.ok` is separate from
the numbers, and an unreadable source drives the targets to the **floor** and
emits an error. Ramping up because a file is missing is how a capacity
controller takes the machine down.

On this machine `/sys/fs/cgroup/agents.slice` genuinely does not exist —
systemd nests the user manager's slice, so the real path is
`/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/agents.slice`. A
bare slice name is therefore resolved against a bounded set of nesting prefixes
rather than assumed to be at the cgroup root.

`fleet serve status` renders a failed reading as a failure, never as `0.0`:

```
CAPACITY
  signal DEGRADED   lanes 1 gates 1 tests 1 typecheck 1
  pressure UNAVAILABLE — targets held at floor, not assumed idle:
    cgroup 'agents.slice' not found under /sys/fs/cgroup (tried: ., user.slice, ...)
```

### Starvation guard

Saturated with nothing completing for `starvation_ticks`, more lanes cannot
help and the expensive thing already in flight — a gate — is what unblocks the
queue. The guard collapses lanes to the floor and sets `gates_priority`.

### The capacity file

The only channel between serve and the components it composes. A component that
wants to be correct cannot be: it must read the file.

```json
{
  "schema": "agent-fleet.serve.capacity/1",
  "operator": "documents-0e",
  "updated_epoch": 1758800000.0,
  "targets": {
    "max_lanes": 6,
    "max_gates": 4,
    "test_pool": 2,
    "typecheck_pool": 2,
    "signal": "hold",
    "reason": "cpu some-avg60 12.0% inside [10.0, 25.0]",
    "gates_priority": false,
    "degraded": false
  },
  "pressure": {
    "ok": true,
    "error": "",
    "path": "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/agents.slice",
    "hierarchy": "v2",
    "cpu": {"some_avg10": 0.0, "some_avg60": 12.0, "some_avg300": 0.0,
            "some_total_us": 6010336491, "full_avg60": 0.0, "full_total_us": 0},
    "memory_used_bytes": 0, "memory_max_bytes": 0, "memory_ratio": 0.33
  },
  "idle_ticks": 0,
  "saturated": false
}
```

`signal` is one of `ease` / `hold` / `pressure` / `unknown`; `degraded` and
`pressure.ok` together say whether the targets came from a real measurement.

A command template can also have the values projected into its argv:

```yaml
components:
  dispatcher:
    command: "fleet dispatch --operator {operator} --max {max_lanes} --gates {max_gates}"
```

Placeholders: `{operator}`, `{serve_dir}`, `{capacity_file}`, `{max_lanes}`,
`{max_gates}`, `{test_pool}`, `{typecheck_pool}`, `{gates_priority}`. Substitution
is plain string replacement, not `str.format`, so a jq filter or a python
one-liner in the command survives. Commands are exec'd **without a shell**.

## The watchdog

Five rules, each a detector plus a bounded remediation, each emitting an event.

| Rule | Fires when | Remediation |
|---|---|---|
| `stuck_stage` | a component's log has not grown past its stage timeout | terminate its group; the owner may retry once, then escalate |
| `orphan_blocking` | a child serve spawned is reparented and older than `orphan_minutes` | terminate its group |
| `stale_lock` | a lock record is held but its holder's pid is gone, past the grace | release the record |
| `deadlock` | two components each hold what the other wants, past the threshold | release the older claim |
| `no_progress` | a component has queued work and has emitted nothing for the window | restart it; escalate once the budget is spent |

Three properties every remediation shares:

- **Only fleet-owned processes**, by exact pid and matching fingerprint.
- **Fail closed, then let the owner retry.** A stuck stage is killed and marked
  dead rather than left running, and a stage that is reliably stuck produces an
  escalation a human can read rather than an infinite kill/retry loop.
- **One budget per tick** (`max_remediations_per_tick`), and the grace before
  escalating TERM to KILL is spent once for the whole tick rather than once per
  victim. Without this, a crash leaving twenty stale children makes one tick
  take minutes, the watchdog falls behind, and it trips its own no-progress
  rule — the watchdog creating the condition it exists to detect. Findings the
  budget did not allow are reported as `deferred`, so silence is never mistaken
  for "nothing was wrong".

### Locks, and why they are recorded

`flock` is mutually exclusive and completely unobservable: the lock file has no
content, so when a merge stopped holding, nothing could say who had it or for
how long, and the deadlock had to be diagnosed by a human reading a log.

So serve keeps the *lock* as a flock — the kernel does the exclusion and drops
it even on SIGKILL — and keeps a *record* beside it:

```
$AGENT_FLEET_HOME/serve/<operator>/locks/<name>.lock   the flock
$AGENT_FLEET_HOME/serve/<operator>/locks/<name>.json   {"state", "holder", "pid",
                                                        "starttime", "acquired_epoch",
                                                        "waiting_for", "wanting"}
```

The `waiting_for` edge is what makes deadlock detection a graph walk rather than
a guess. Records are advisory metadata: if one is wrong the kernel still
prevents double-holding, and if one is missing the lock is merely untracked.

## Escalation routing

The gate and the lane runner both end items at `NEEDS-ESCALATION <reason>`, for
four quite different reasons. Routing them all one way is what makes an
operator's queue unusable, so the reason is classified and the class decides the
action:

| Class | Action | Why |
|---|---|---|
| `infra` | retry **once** automatically | a broken environment, not broken work. A second failure means the machine needs a human, and a retry loop only multiplies the damage |
| `untestable` | fix round | a real defect with no testable shape — ordinary work |
| `fence`, `owner_decision` | a human decides, in batches | re-running something a human fenced is the most expensive possible mistake |
| *unrecognised* | a human decides | see below |

**An unrecognised reason routes to a human, never to an automatic retry.** The
gate emits free text and no reason class exists anywhere else in the codebase, so
this code will meet reason strings it has never seen. Guessing `infra` for one
would give it an automatic retry — and if it turned out to be a fence, that
means automatically re-running something a human deliberately stopped.

A reason may carry an explicit token, which wins over marker matching:
`class=infra`, `class=owner_decision`, `[class: untestable]`.

Decisions are an append-only file, not a ping. Per-item notification is how a
fleet of this size becomes unmonitorable: a hundred notifications train a human
to mute the channel, and then the one that mattered goes unread. A human
resolves a decision by appending a resolution record.

## `fleet serve status`

One screen: components (up/restarts/adopted), capacity targets against pressure,
queue depth by stage, per-hour throughput, and the oldest item per stage with
why it waits. `--json` is the same dict the text renders from, so the two cannot
disagree.

```
fleet serve — operator documents-0e
========================================================================

COMPONENTS
  - dispatcher STOPPED       restarts 0   no pid
  - merger    RUNNING        restarts 2   pid 48213

CAPACITY
  signal HOLD       lanes 6 gates 4 tests 2 typecheck 2
  pressure cpu some-avg60 12.0  memory 33%
  cgroup /sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/agents.slice

STAGES
  stage      depth    /hr  oldest       age  why it waits
  -------------------------------------------------------
  queued         4    2.0  lane-ws-2    37m  gates_priority: holding new lanes at floor
  gating         3    1.0  lane-docs-1   12m  waiting for a gate slot (max_gates=4, gating 12m)
  merged         2    1.0  lane-fix-3      -  in merged for 0m
```

## Configuration

The section is `serve:` at the top level of the **global** `fleet.yaml`, because
serve supervises processes across repos. A repo may also carry
`fleet_ops.serve` in its `.agent-fleet.yaml`, because that is where an operator
looking at a repo will look for it.

An explicitly named config file with no `serve:` section is an **error**, not a
silent fall back to defaults. A supervisor quietly running on thresholds the
operator believes they configured is the failure this prevents.

```yaml
serve:
  tick_seconds: 15
  shutdown_grace_s: 10
  cgroup: agents.slice          # bare name resolved under systemd's nesting
  watchdog_every_ticks: 1
  throughput_window_hours: 1

  capacity:
    psi_low: 10                 # below -> add
    psi_high: 25                # above -> drop; between -> hold
    memory_low_ratio: 0.70
    memory_high_ratio: 0.85
    floors:   {lanes: 1, gates: 1, tests: 1, typecheck: 1}
    ceilings: {lanes: 20, gates: 12, tests: 6, typecheck: 6}
    step: 1
    decrease: 0.5
    starvation_ticks: 6

  components:
    dispatcher:
      command: "fleet dispatch --operator {operator} --max {max_lanes}"
      backoff_initial_s: 5
      backoff_max_s: 300
      timeouts:
        crash:       {threshold: 5, window_minutes: 15}
        no_progress: {restarts: 2, window_minutes: 30}
    merger:
      command: "fleet merge run --operator {operator} --capacity {capacity_file}"
    janitor:
      command: "fleet serve janitor --operator {operator} --once"

  watchdog:
    orphan_minutes: 60
    stale_lock_minutes: 15
    deadlock_minutes: 20
    no_progress_minutes: 30
    stage_retry_budget: 1
    kill_grace_s: 5
    remediation_budget: {max_per_tick: 5}
    timeouts:                 # per stage, in minutes
      lane: 180
      gate: 90
      fix: 60
      rebase: 45
      merge: 60
```

Every threshold has a safe default: floors rather than ceilings, a 20-minute
stage timeout, and a crash-loop budget that trips before a broken component is
restarted a hundred times. A repo with no `serve:` section is unaffected.

Note that `serve` does **not** register its own `--config`. The top-level parser
already defines it, and argparse resolves a subparser's default onto the same
`dest` — so a second `--config` would silently discard the value passed as
`fleet --config X serve` and fall back to the global `fleet.yaml`. Use the
top-level flag, or `--serve-config` for a serve-specific override.

## State on disk

```
$AGENT_FLEET_HOME/serve/<operator>/
    serve.pid              the supervisor's own pid + fingerprint
    serve.lock             flock held for the supervisor's lifetime
    state.json             components, crash history
    capacity.json          the AIMD targets components read
    items.jsonl            the stage transition log (append-only)
    events.jsonl           serve's event mirror
    decisions.jsonl        fence/owner escalations awaiting a human
    components/<name>.log  each component's stdout+stderr
    components/<name>.pid  each component's pid + fingerprint
    locks/                 lock flocks and their records
```

Everything is scoped by operator so two supervisors can run against the same
repos without either clobbering the other's capacity targets or killing the
other's children.

Events go to the shared fleet stream (`$AGENT_FLEET_RUNS_DIR/serve-<operator>.jsonl`
as real `FleetEvent`s, so existing tooling keeps working) *and* to the local
mirror, because a supervisor that is down cannot write to a stream another
process may be rotating.

## What this replaces

| Serve component | Replaces |
|---|---|
| `dispatcher` | `dispatch.py` — the lane dispatcher loop |
| `merger` | `automerge2.sh` — approved-lane collection, batching, rebase-on-conflict |
| `janitor` + watchdog | `watchdog.sh` — stall and orphan detection |
| escalation routing | `fix_untestable.sh` — the untestable fix round |
| escalation routing | `rebase_regate.sh` — the rebase-and-regate round |
| `capacity.json` | the ad-hoc `--max` / `--max-gates` / `--max-load` flags and `os.getloadavg()` in `dispatch.py` |
| `items.jsonl` | `lanes/*.status` grepping, `automerge.hold`, and the per-lane `.status` files |
| `fleet serve status` | `fbstatus`, `snapshot_lanes.sh`, and reading `events.log` by hand |

`fbgate` itself is not replaced — it is the gate, and it keeps being the gate.
Serve supervises the thing that invokes it, sizes how many run at once, and
routes what it escalates.

## Running it

`fleet serve` is a foreground process, like every other long-running fleet
command. It installs `PR_SET_PDEATHSIG` so its children die with it if it is
`SIGKILL`ed, and cleans up its pid file and children in a `finally` on
`SIGTERM`.

Ops automation — a systemd unit or timer to keep it running — belongs to
whoever owns this machine's service management; `fleet serve` deliberately
installs nothing itself.
