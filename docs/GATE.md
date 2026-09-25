# The `gate` pipeline — evidence-based pre-merge approval

`agent-fleet gate --repo-path <path> --pr <n>` decides whether a pull request is
safe to merge, and answers with **evidence** rather than opinion.

The gate's central rule: **a claim is not a blocker until a test demonstrates
it.** Reviewers propose; a test disposes. The only thing the gate ever approves
is a commit where the deterministic test set is green and every blocker it found
has been resolved.

```bash
agent-fleet gate --repo-path /path/to/repo --pr 42 \
  --task-file /path/to/task.md \
  --status-file /path/to/lane.status
```

Exit code is `0` only on `APPROVED`. `NEEDS_ESCALATION`, an unusable PR, and a
deterministic step that could not run all exit `1`, so a wrapper can gate on the
exit code alone as well as on the status line.

---

## Why find → verify → one-fix beats iterative review

The obvious design is a review/fix loop: a reviewer comments, a fixer patches, a
reviewer re-reads, repeat until someone is satisfied. It fails in two specific
ways, and the gate is shaped to avoid both.

**Reviewer verdicts are not evidence.** A reviewer that says "this looks wrong"
may be right, may be reading a stale file, or may be describing a risk that does
not exist. An iterative loop has no way to tell these apart, so it either trusts
the reviewer (and merges a real bug) or argues with them (and burns a day on
nothing). The gate resolves the disagreement *mechanically*: a verifier must
write a test that fails, and the **pipeline re-runs that test itself**. If pytest
exits 1, the claim is real. If the test passes, the pipeline discards the claim
regardless of what the verifier said. The test outranks the opinion.

**Fixing what was never wrong wastes the budget.** When a fixer receives a list
containing one real defect and nine nits, it learns to negotiate with the list
rather than fix the code. So the gate asks reviewers for **blockers only** — an
empty findings list is an explicit, normal, good outcome — and hands the fixer
only tests that actually fail.

The gate also inverts the usual trust direction for the expensive step. Instead
of a cheap reviewer gating an expensive fixer, cheap parallel lenses produce
candidates, and *nothing is dispatched for repair* until a deterministic test
has demonstrated a failure. On a clean PR the gate costs one deterministic test
run and approves; it never spends a fixer at all.

### Why convergence instead of a round cap

The original pilot ran **exactly one** fix round. That is simple and it is wrong
in both directions: a PR with two independent defects always escalates, and a PR
where the fixer makes one small step forward is indistinguishable from a PR that
is genuinely stuck.

So the gate measures progress instead of counting rounds. Each round:

1. dispatches **one** fix against the tests failing *right now*,
2. re-runs **all** gate tests plus the PR's own tests at the new head, and
3. compares the failing set to the previous round's.

A round counts as progress only when the failing set **strictly shrinks** *and*
**no new failures appear**. Then:

| Condition | Outcome |
|---|---|
| 0 failing | `APPROVED` |
| Set strictly shrank, nothing new broke | next round |
| Nothing fixed, or the set did not shrink, or something new broke | `NEEDS_ESCALATION` (stalled) |
| Fixer pushed nothing | `NEEDS_ESCALATION` (no-push) |
| Tests could not run at the new head | `NEEDS_ESCALATION` (tests-broken) |
| Untestable blockers still unresolved | `NEEDS_ESCALATION` (untestable-unresolved) |

`max_fix_rounds` (default 4) is **only a safety net** for a run that keeps making
one-test-at-a-time progress without converging. It is not the stopping rule, and
hitting it is reported as `cap` — a distinct outcome from a genuine stall, so the
metrics show which happened.

---

## The steps

### step0 — the PR's own tests at head

Before any model is consulted, the gate runs the test files the PR actually
changed, grouped by the package that owns them (`pyproject.toml`-dir aware, so a
multi-package repo runs pytest from the right directory for each package). Every
failure is a confirmed blocker with no interpretation step at all.

The exit code distinction is load-bearing:

- `0` — passed.
- `1` — tests failed. A real finding.
- `>= 2` — collection error, usage error, interruption, or timeout. **An infra
  error, never a finding**: the suite did not run, so the gate learned nothing
  about the code. The run escalates rather than guessing.

Deliberately narrow: only the PR's *own* changed tests. Widening to the whole
suite would turn one slow package into a gate timeout for reasons unrelated to
the change.

### find — parallel lens reviewers

N lens reviewers run concurrently, each with exactly one focus. The defaults are
`correctness`, `contract`, `prodsafety`, and `spec`; the set and each focus are
configurable. Each returns structured JSON (validated against
`schemas/gate_findings.schema.json`) containing only blockers, each with an exact
`file`/`line` and a **concrete repro** — the input/state and the observable wrong
outcome, precise enough that someone can write a failing test for it.

Duplicate claims across lenses are de-duplicated on (file, claim) so a defect
four reviewers rediscovered is verified once.

### verify — one failing test per claim

One verifier per testable claim. It must create **exactly one new test file**,
exercise the real code path, and fail *on an assertion about the claimed
behaviour* — not from an import error, missing fixture, or environment problem.

The pipeline then re-runs that test itself:

| Verifier said | The test... | Result |
|---|---|---|
| `CONFIRMED` | exits 1 (test failure) | **blocker**, archived for the fix rounds |
| `CONFIRMED` | exits 0 | discarded — the pipeline believes the test, not the verdict |
| `CONFIRMED` | could not run / file missing | discarded (counted as rejected) |
| `REJECTED` | — | rejected, with the reason |
| `UNTESTABLE` | — | routed to the judge |

A claim the reviewer marked `"testable": false` skips verification entirely and
goes to the judge.

### judge — one call, and no free passes

At most **one** judge call, on a separately configured backend/model, which does
two jobs:

1. Rules on each `UNTESTABLE` claim: is it a real merge blocker in the current
   code? Only a `"real": true` ruling is promoted to a blocker.
2. Runs **one blocker pass of its own**.

The judge's new blockers get no special treatment — they go back through
`verify`, so the expensive model cannot assert a blocker either. If the judge
call fails outright, its claims stay unresolved; a failure is never read as an
all-clear.

### converge — fix rounds on a shrinking failing set

Each round creates a fresh detached worktree at the current head, materialises
the archived gate tests into it, and dispatches **one** fixer with:

- the tests failing *right now*, which must pass,
- the confirmed defect descriptions behind them,
- every other listed test, which must keep passing, and
- the instruction to keep the gate tests and never weaken their assertions.

The fixer commits and pushes to the PR branch. The gate re-reads the head and
re-runs the full deterministic set. Archived gate tests are copied back into
every new worktree, because the fixer pushes product code and those test files
would otherwise disappear.

When the failing set reaches zero but untestable blockers remain, the gate makes
its **one recheck call** to the judge: are they resolved at the new head? Report
only unresolved ones. A failed recheck is not a pass.

### outcome

`APPROVED(sha)` or `NEEDS_ESCALATION(reasons)`, written to the JSONL run log
and — when `--status-file` is given — as one line:

```
HH:MM:SS PREMERGE-APPROVED <sha9>
HH:MM:SS NEEDS-ESCALATION <first reason>
```

That single line is what the automerge watcher reads.

The status file has a third line type, written by the *lane* rather than by the
gate: `HH:MM:SS GATE-SKIPPED PR #<n> @<sha9> (<reason>)`. It means the PR was
guaranteed but the gate did not run — `--no-gate`, or no gate installed. It is
not an escalation and not an approval; `gate.is_approval_line` does not match
it, so a `GATE-SKIPPED` line can never be read as a gate that cleared a PR.

---

## Machine-wide admission

The gate never works in the caller's checkout. Every step runs in a detached
worktree created at an explicit sha, so a run that crashes mid-round leaves a
directory behind but no half-updated branch, and the next run replaces it.

**Cross-process slots.** Several independent fleet processes (this gate, a watch
daemon, an ad-hoc `fleet run`) would each get their own in-process concurrency
budget, so N processes could fan out to N × `max_parallel` agents. Instead every
backend session holds a slot from a shared pool under `~/.agent-fleet/slots`:

```
~/.agent-fleet/slots/agent/slot.0 … slot.23   + pool.json
~/.agent-fleet/slots/test/slot.0  … slot.3    + pool.json
```

A slot is a file descriptor held with an advisory `flock`. The kernel drops the
lock when the holding process exits — however it exits, including `SIGKILL` — so
a crashed gate run cannot leak slots. There is no cleanup bookkeeping, no
stale-pid reaping, and no lock file to corrupt. Sizing is set by `agent_slots`
and `test_slots`.

**The test pool is deliberately much smaller than the agent pool.** Agents are
cheap in memory; pytest is not. A runaway suite once consumed 36GB on this
machine.

**Memory-capped test runs.** Every pytest the gate launches is wrapped in
`systemd-run --user --scope -p MemoryMax=<test_memory> -p MemorySwapMax=0` when
systemd user scopes are available, and each holds a test-pool slot for its
duration. The cap is used exactly as configured; the gate never raises it.

---

## Model policy

`model_policy` in the machine-wide `fleet.yaml` pins which models each backend
may spend and which pipeline roles a backend may serve. The gate checks it
**before dispatching any agent**, so a config drift or typo fails in a second
rather than after a fan-out has already spent the budget.

```yaml
model_policy:
  backends:
    cmd:
      allowed_models: ["stealth/space-bunny-alpha"]
    grok:
      allowed_models: ["step-5-preview"]
      roles: ["judge"]
```

- `allowed_models` — the only models that backend may use. A backend absent from
  the policy is unrestricted, but must still be given an explicit model.
- `roles` — restricts a backend to specific roles. This is what keeps the
  expensive judge model out of the cheap parallel fan-out.

`examples/fleet.gate.yaml` ships exactly this policy plus a fully commented gate
config.

---

## Configuration

Machine-wide (`~/.agent-fleet/fleet.yaml`):

| Key | Default | Meaning |
|---|---|---|
| `model_policy.backends.<name>.allowed_models` | — | models that backend may use |
| `model_policy.backends.<name>.roles` | any | roles that backend may serve |
| `gate.backend` / `gate.model` | `cmd` | backend + model for find/verify/fix |
| `gate.judge_backend` / `gate.judge_model` | `grok` | backend + model for the judge |
| `gate.lenses` | 4 defaults | lens name → focus text (or a list) |
| `gate.max_candidates` | `12` | cap on claims carried into verify |
| `gate.max_parallel_lenses` | `8` | concurrent lens reviewers |
| `gate.max_parallel_verifiers` | `6` | concurrent verifiers |
| `gate.max_fix_rounds` | `4` | **safety net only**, not the stopping rule |
| `gate.enable_fix` | `true` | run fix rounds at all |
| `gate.enable_judge` | `true` | run the judge call |
| `gate.base_branch` | `main` | base for the diff reviewers read |
| `gate.push_branch` | PR head | branch the fixer pushes to |
| `gate.agent_timeout_s` | `1800` | per-agent timeout |
| `gate.judge_timeout_s` | `7200` | judge timeout |
| `gate.test_timeout_s` | `900` | per-pytest timeout |
| `gate.test_memory` | `6G` | `MemoryMax` for every pytest |
| `gate.agent_slots` | `24` | machine-wide agent slot count |
| `gate.test_slots` | `4` | machine-wide test slot count |
| `gate.package_dir` | auto | force all tests into one package dir |

`gate: false` disables the gate entirely. An absent or malformed section falls
back to the documented defaults, so `agent-fleet gate` works in a repo with no
config at all.

---

## Outputs

**JSONL run log** — every run emits `gate.start`, `gate.step0`, `gate.find`,
`gate.verify.*`, `gate.judge`, `gate.recheck`, `gate.round`, and `gate.outcome`
events into the standard fleet run log.

**Status file** — the one line the automerge watcher reads, described above.

**Metrics** — one JSONL row per run at `~/.agent-fleet/gate/metrics.jsonl`
carrying the full funnel (`candidates`, `confirmed`, `rejected`, `untestable`,
`untestable_real`), the per-round convergence trace, and the terminal outcome.
Because `max_fix_rounds` is only a safety net, this is what makes convergence
observable — a run that stalled after three rounds of one-test progress is
distinguishable from one that converged in a single round.

```bash
agent-fleet gate metrics --format table
```

```
AT                   PR  CAND  CONF  REJ  UTEST  RND  FAILING      OUTCOME
-----------------------------------------------------------------------------
2026-09-25T10:00:00   42     9     3    6      1    3  5,3,0        converged
2026-09-25T11:14:02   43     4     1    3      0    2  2,2          stalled
```

```bash
agent-fleet gate metrics --limit 50   # JSON: recent rows + aggregates
```

---

## What the gate does not do

- It does not merge. It approves or escalates; automerge reads that.
- It does not run the full test suite — only the PR's changed tests plus the
  gate's own, so a slow unrelated package cannot consume the gate's budget.
- It does not judge style, naming, docs polish, or "could be cleaner". Those are
  not blockers and are excluded by construction.
- It does not loop on review opinion. There is no re-review step, because a
  re-review is another opinion, and the gate's evidence is a test.
