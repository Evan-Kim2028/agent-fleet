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

Once a PR has been approved and then rebased onto a moved `main`, the change is
often identical to what was approved. `gate recheck` re-establishes that cheaply
instead of paying for a full review again — see [carrying an approval across a
patch-identical rebase](#carrying-an-approval-across-a-patch-identical-rebase):

```bash
agent-fleet gate recheck --repo-path /path/to/repo --pr 42 \
  --approved-sha <approved-head> --status-file /path/to/lane.status
```

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
| 0 failing, untestable blockers open | one untestable fix round, then the recheck judge — see below |
| Untestable blockers still unresolved after that round | `NEEDS_ESCALATION` (untestable-needs-review) |

`max_fix_rounds` (default 4) is **only a safety net** for a run that keeps making
one-test-at-a-time progress without converging. It is not the stopping rule, and
hitting it is reported as `cap` — a distinct outcome from a genuine stall, so the
metrics show which happened.

---

## Untestable blockers get a fix round

Some confirmed blockers cannot be shown by a test: a documentation file that
contradicts the shipped behaviour, a script whose arguments do not do what its
`--help` says. The judge can rule them real, and the gate counts them as
blockers — but the convergence rule above is defined on a *shrinking failing
set*, and a green test set gives it nothing to measure.

That used to mean the gate escalated on first sight. The defect was real and
confirmed, and nobody had been asked to fix it. The gate said "untestable
blocker(s) need human review" about work that a fixer could have done in one
commit.

It now works like the reference bash gate's `fix_untestable.sh`:

1. A green test set with open untestable blockers runs **exactly one** fix round,
   with the untestable list in the fixer's prompt.
2. The **recheck judge** — not the fixer — decides whether they are resolved.
3. Resolved → `APPROVED`. Still open, or the fixer pushed nothing →
   `NEEDS_ESCALATION` (`untestable-needs-review`) naming the blockers.

**One round, not a loop.** With no test to turn green there is no measurable
progress, only the judge's yes/no, so raising `max_fix_rounds` does not buy more
attempts at it. A fixer never approves its own work: only a recheck that reports
nothing unresolved does. A recheck that could not answer is not a pass.

`enable_fix: false` keeps its meaning — no fixer is dispatched at all, and the
gate escalates immediately as before.

---

## Gate test files are unique per PR

A verifier writes its failing test **into the PR's repository**, so the file
name has to be unique per PR. It is:

```
test_gate_<lane_slug>_<finding_id>.py      # e.g. test_gate_fb_gaterobust_contract_1.py
```

The slug comes from `--lane-slug`, else `gate.lane_slug`, else the PR's own head
ref. Both halves are folded to alphanumerics and underscores and bounded, so a
branch like `fb/a.b/c` produces `fb_a_b_c` rather than a path.

This is not cosmetic. With a fixed name, two PRs gating different branches both
produced `tests/test_gate_contract_1.py`; merging one into main turned the other
into an **add/add conflict**, which is what forced a rebase and a full re-gate
for every PR that landed after it. Because the slug is in the name, the
conflict cannot arise, and [carrying an approval across a
rebase](#carrying-an-approval-across-a-patch-identical-rebase) can exclude these
files from its patch comparison for the same reason.


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

---

## No blocking commands, and per-stage timeouts

Every gate prompt — lens, verifier, judge, recheck, and fixer — opens with the
same two-part preamble:

- **No blocking commands.** Never run a command that waits forever: `tail -f`,
  `journalctl -f`, `watch`, an interactive editor or pager, a sleep loop with no
  exit condition, or a server in the foreground. To watch something, poll with a
  bounded loop that has a timeout and an exit condition.
- **Process safety.** Never kill processes by name or pattern; record the PID of
  anything you start and kill only that.

The first exists because a command that never returns is not a slow stage, it is
a **dead** one: the slot is held, the answer never arrives, and the stage burns
its whole budget before the run escalates. The gate already failed closed in
that situation, so the run was safe — just wasted. Both rules live in one shared
prefix (`AGENT_RULES`) rather than being restated per role, so a role added later
inherits them by construction.

### Per-stage budgets

Each agent stage gets its own budget. One shared number was wrong in both
directions: a fixer that commits, pushes and waits on a test suite never fits in
a reviewer's budget, and a review that is going to produce nothing is still
allowed half an hour before anyone notices.

| Stage | Default | Why |
|---|---|---|
| `lens` | 40 min | read the diff, form claims |
| `verify` | 40 min | write and run one failing test |
| `judge` | 40 min | rule on untestable claims + one blocker pass |
| `fix` | 90 min | edit, run tests, commit, push |

Set them with `gate.lens_timeout_s` / `verify` / `judge` / `fix`. The older
`gate.agent_timeout_s` is **deprecated but still honoured**: it is applied to the
three stages it used to drive, with a warning naming the replacement, and an
explicit per-stage key always wins — so an existing `fleet.yaml` loses nothing
and the deprecation can be resolved one stage at a time.

### A stage timeout is a dead agent

An agent that ran out of time produced **no evidence either way**, so a timeout
fails closed exactly like a crash. That was already true; what was missing was
naming it. Exit 124 is now classified as its own failure kind, and the
escalation says which stage, the budget it blew, and how long it actually ran:

```
fail-closed: lens stage for correctness timed out after 2412s
(stage budget 2400s); no verdict was produced
```

That matters because a timed-out lens reporting `candidates=0` is
indistinguishable from a clean review — the exact bug class that once approved a
PR carrying three real blockers. The fixer is covered too: it has no JSON schema,
so its timeout is visible only from the backend's exit code, and without this it
was merely logged before the round carried on to report the misleading
`no-push`.

Raise the stage's budget if the escalation is one you want retried; the reason
tells you which one.

---

## Carrying an approval across a patch-identical rebase

When a PR must be rebased onto a moved `main`, the change is often **identical** —
same diff, new parent commit. Paying for a full find → verify → judge run to
rediscover that is waste, and it is the common case, because the thing that most
often forces a rebase is `main` moving.

```bash
agent-fleet gate recheck --pr 42 --approved-sha <old> --head <new> \
  --status-file /path/to/lane.status
```

`recheck` is deterministic and dispatches **no agent**: it re-runs the PR's own
changed tests plus the archived gate tests on the new head with the current base
merged in, and compares the change's identity.

**Is it the same change?** `git patch-id` over
`merge-base(base, sha)..sha`, excluding `test_gate_*` files. patch-id hashes the
diff rather than the commit, so it survives re-parenting; the gate tests are
excluded because they are evidence the gate writes into the PR's repo, and a
collision on one is what routinely forced the rebase in the first place (see
[above](#gate-test-files-are-unique-per-pr)).

All four of these are required:

| Condition | Otherwise |
|---|---|
| `--approved-sha` names a real commit | `full gate required: … is not a known commit` |
| the status file has a `PREMERGE-APPROVED` line for it | `full gate required: no PREMERGE-APPROVED line for …` |
| the patch-id is unchanged | `full gate required: change differs from the approved patch` |
| every test passes on the rebased head | `full gate required: N test(s) fail …` |

A pytest **infra error** — a suite that could not run at all — is never a
carry-over. Neither is a missing git call or an uncreatable worktree: a recheck
that cannot establish its verdict must not produce one.

On success the status line names the **new** head, since that is the commit the
automerge takes:

```
10:00:00 PREMERGE-APPROVED 4f2a1b9c3
```

with the reason `approval carried over from 9e1c…: patch-identical
(patch-id 3ab81f0c…, gate tests excluded) and all 12 test(s) green on the rebased
head`. The metrics row is marked as a recheck rather than a gate run, so a
carried approval is not mistaken for a reviewed one.

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
| `gate.lane_slug` | PR head ref | slug in gate test file names (per-PR uniqueness) |
| `gate.lens_timeout_s` | `2400` | lens stage budget |
| `gate.verify_timeout_s` | `2400` | verifier stage budget |
| `gate.judge_timeout_s` | `2400` | judge / recheck stage budget |
| `gate.fix_timeout_s` | `5400` | fix stage budget |
| `gate.agent_timeout_s` | — | **deprecated**; still applied to lens/verify/fix |
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

**Per-call artifacts** — every lens, verifier, judge, recheck and fix call writes
`<gate_dir>/calls/<stage>-<n>.json` holding the raw final text, the parsed
object, the parse error, the exit code and the duration. Failures are recorded
too: a dead or unparseable reviewer is exactly the case where the raw text is
the only evidence of what happened. This is what makes a `candidates=0` result
diagnosable — you can tell a reviewer that returned nothing from findings that
were lost between the agent and the counter.

```bash
ls .agent-fleet/gate/3541/calls/
cat .agent-fleet/gate/3541/calls/lens-1.json | jq '{raw_len, parsed_ok, n_items, parse_error}'
```

**Metrics** — one JSONL row per run at `~/.agent-fleet/gate/metrics.jsonl`
carrying the full funnel (`candidates`, `confirmed`, `rejected`, `untestable`,
`untestable_real`), the per-round convergence trace, the terminal outcome, and a
`calls` array with the per-call parse state (`raw_len`, `parsed_ok`, `n_items`,
`parse_error`) for each reviewer. The same summary appears under
`funnel.lens_calls` in the run's result JSON. Because `max_fix_rounds` is only a
safety net, this is what makes convergence observable — a run that stalled after
three rounds of one-test progress is distinguishable from one that converged in
a single round.

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
