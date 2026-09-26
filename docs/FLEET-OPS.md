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
7. **Status line** — appended to `--status-file` as one of three lines:
   `HH:MM:SS PREMERGE-APPROVED <sha9>`,
   `HH:MM:SS NEEDS-ESCALATION <reason>`, or
   `HH:MM:SS GATE-SKIPPED PR #<n> @<sha9> (<reason>)`.
8. **Hooks** — the operator's `on_approved` or `on_escalated` command.

### Run logs never reach the branch

The engine writes its JSONL stream and final text to
`~/.agent-fleet/runs/<operator>/<lane>/<run-id>/` — **outside** the lane
worktree. Two things depend on that:

* the guarantee stages with `git add -A`, so anything inside the worktree
  becomes part of the PR unless it is excluded;
* one directory per lane meant a second run of the same lane overwrote the
  first run's transcript — the artifact an operator needs when a lane has to be
  explained afterwards.

Belt and braces: `ensure_lane_worktree` also adds `.agent-fleet/runs/` to the
repo's `info/exclude` (idempotently, on every path including worktree reuse),
and the guarantee takes that subtree back out of the index after its
unconditional `git add -A`. The two overlap on purpose — the exclude file
covers a caller who passed an explicit run dir, and the unstage covers a repo
whose exclude file could not be written.

The exclude line names the *logs subtree*, not `.agent-fleet/`. A repository in
this fleet tracks `.agent-fleet/` as real config, and a blanket ignore hides
files a lane creates there from `git status` entirely — an invisible file is
never staged, never committed, and a lane whose only work was there read as a
clean worktree.

This is what stopped a lane that changed *nothing* from being reported as
`commit_failed`: the run log used to be the only untracked file in the
worktree, so the guarantee staged it, committed only it, and died on the
repo's hooks.

### When the implementer produces nothing

A lane whose worktree is clean and whose branch is ahead of nothing has no PR
to guarantee. That is not a plumbing failure, and the manager now says which of
the two real things happened:

| Reason | Meaning | Automatic action |
| --- | --- | --- |
| `no_changes_stopped` | the implementer **decided** to stop — a fence, an owner decision, it needed clarification — and explained why | none; the reason is in `detail` and on the status line, for a decision list |
| `lazy_exit` | the final text is short and phrased as work *about to happen* ("Now I'll update the manifest:") | one automatic retry with a nudge, then escalate if it is still lazy |
| `no_commits_ahead` | there was no final text to judge at all | none |

The implementer's own final message (the last ~1500 characters — the part where
a model that reached a decision explains itself) goes into `detail` and into the
status line, so an orchestrator can route it without opening the transcript.

The lazy heuristic is deliberately biased against retrying: a final text that
*claims completion* ("opened the PR") is not treated as unfinished, and an
ambiguous mix resolves to not-retrying. An engine narrates a completion it did
not achieve far more often than it stops mid-sentence, and a needless retry
costs a full engine run.

The phrase alone is not enough either. An implementer that decided to stop says
so in the first person all the time — "I'll hold off until the owner decides",
"I am going to wait for the owner" — and those announce the decision it already
took, not work it failed to do. The announcing phrase only counts when the text
also *ends* on a fragment (a colon or an ellipsis), which is what a cut-off
mid-thought looks like; a sentence that ends in a full stop has run out of
sentence, not of steam.


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

When a hook does refuse, the failing hook **ids** are parsed out of pre-commit's
`- hook id: <id>` blocks and reported as `hooks_failed=[...]` — on
`LaneRunResult.hooks_failed`, in the escalation `detail`, and in the status
line. The transcript is long and truncated at an arbitrary offset; the ids are
the one part that says which hook to fix, so they lead.

### The status-line vocabulary

Three lines, appended to the status file:

```
HH:MM:SS PREMERGE-APPROVED <sha9>
HH:MM:SS NEEDS-ESCALATION <reason> [hooks_failed=[...]] [the implementer's reason]
HH:MM:SS GATE-SKIPPED PR #<n> @<sha9> (<reason>)
```

`GATE-SKIPPED` is its own token rather than an escalation because it is not
one: the PR is guaranteed, the state is `pr_guaranteed`, `approved` is `False`,
and an external gate owns review from there. Writing it as `NEEDS-ESCALATION`
made a healthy lane read as a broken one. `gate.is_approval_line` is the single
definition of what counts as an approval, and a `GATE-SKIPPED` line does not
satisfy it.

`statusfile.last_status_line(path, tokens=...)` treats a filter naming exactly
the two legacy tokens as the pre-`GATE-SKIPPED` contract and widens it to the
whole vocabulary, so such a consumer keeps reading *this* run's terminal line
rather than falling through to a verdict an earlier run already superseded. The
widened match reads each line's verdict *field* rather than the whole line: the
escalation line carries the tail of the implementer's own final message, and an
unanchored search would let model-authored text forge a verdict.


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

The manager instead refuses, rather than guessing, in these cases:

| Refusal | Why |
| --- | --- |
| `refused_own_pgid` | the recorded pgid is the caller's own group — never signal it |
| `refused_pid_reused` | the pid's start-time fingerprint changed, or the record carries none |
| `refused_ambiguous_lane` | two operators own this lane name — pass `--operator` |
| `refused_lane_not_found` | no such lane |
| `refused_no_process_identity` | the record names no process at all — the lane is already over |

A lane that is already gone reports `already_gone` as success, not failure.

A fingerprint the machine cannot supply is a refusal, not a gap in the check:
there is nothing to match the recorded pid against, so "this number is the same
process that wrote this record" cannot be established, and the only outcome of
getting it wrong is `killpg` on someone else's tree. Every writer of the record
sets `pid`, `pgid` and `starttime` together, and every escalation clears all
three, so a missing fingerprint means a torn or hand-edited record.

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
against a lane that is now managed. `GATE-SKIPPED` is a third, *additional*
line type: a consumer that only looks for the first two sees exactly what it saw
before — and, because `last_status_line` widens that two-token filter to the
whole vocabulary, it still sees *this* run's terminal line rather than the one an
earlier run left behind.

A sha that is not a real short sha is never written as an approval: the old
automerge took the last field of the line and would otherwise try to merge a PR
whose head started with that word, find none, and loop.

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
| `cli.py` | `lane run`, `lanes status`, `lanes stop` |

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
