# ADR 0002 — Autonomy control plane for PR loop

**Status:** Accepted  
**Date:** 2026-07-08

## Context

PR loop merge/fix decisions were scattered across `lifecycle.py` and
`watcher.py`: `has_blocking_findings`, `review_addressed` (boolean, not
SHA-keyed), `tiered_merge_gate`, and protected-path parking in `try_merge`.
That made it hard to reason about invariants (critical paths must never
auto-merge; MEDIUM risk must not merge unless addressed for the *current*
head; CI red must not merge).

## Decision

Introduce a pure module `agent_fleet/autonomy/`:

| Piece | Role |
|-------|------|
| `types.py` | `Finding`, `ReviewEvidence`, `CiEvidence`, `PathEvidence`, `AutonomyEvidence`, `Action`, `Decision` |
| `parse_review.py` | Comment body → `ReviewEvidence` (regex parity with `review_parse`) |
| `decide.py` | `decide(evidence) → Decision` — single policy function |

### Evaluation order in `decide()`

1. **PARK** if any changed file matches `critical_prefixes` (I1).
2. **WAIT_REVIEW** if review evidence is missing/empty.
3. SHA-keyed address: `review_addressed_for_sha == pr_head_sha` clears
   blocking for that head only (I3). Review `head_sha` ≠ PR head invalidates
   address.
4. **FIX_REVIEW** if MEDIUM+ risk/counts and not addressed (I2).
5. Security category MEDIUM+ never yields **MERGE** (fix or park).
6. **FIX_CI** if CI not green; **NOOP** if CI pending (I4).
7. **MERGE** only when review is clear (or addressed for this SHA) and CI green.
8. Else **NOOP**.

Lifecycle and watcher *project* `Decision.action` into fix loops, park
comments, and merge attempts. Markdown review comments remain a projection
of evidence; policy does not parse ad hoc in merge paths when
`pr_loop.use_autonomy_decide` is true (default).

### State

PR state stores `review_addressed_for_sha` (and keeps `review_addressed` for
backward compatibility). Unparking on new commits clears both.

## Consequences

- Invariants I1–I4 are unit-tested against pure `decide()` without GitHub.
- Merge is admitted only when `decide` returns `MERGE`, even if
  `tiered_merge_gate` is false (residual MEDIUM blocks unless addressed for
  the current SHA).
- Further phases (category skip lists, soft verify) can extend evidence types
  without rewiring lifecycle branches.
