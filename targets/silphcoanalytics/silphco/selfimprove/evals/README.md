# Self-Improvement Loop — Eval Corpus

This directory holds the `promptfoo` evaluation configuration used by
`gate.py` as a regression gate before any self-improvement PR is opened.

## Why two categories of test cases?

| Category | Purpose |
|---|---|
| `target_signature` | Verify the proposed change actually improves the failure class it was designed to fix. |
| `frozen_success` | Verify the change does not regress previously-passing behaviour. |

The proposer (`propose.py`) **never has access** to this directory. `gate.py`
reads it; `propose.py` does not import from `evals/`. This separation is
enforced by construction — there is no import path from proposer to evals.

## Growing the corpus

After each accepted self-improvement PR, add real failure traces as new test
cases:

1. Find the run trace in `data/events/agent_runs/<YYYY-MM-DD>.ndjson`.
2. Copy the relevant records.
3. Add a `target_signature` test case whose `prompt` reproduces the failure
   context (persona + phase + the kind of input the agent sees).
4. Add a `frozen_success` test case for the same persona + phase with input
   that was working before.
5. Write `assert` blocks that express what "passing" looks like:
   - For `target_signature`: the LLM now produces the correct artefact.
   - For `frozen_success`: the artefact is still produced correctly.

### Example minimal case

```yaml
- description: "[frozen_success] backend/verify/schema_validation_failed — previously passing trace"
  vars:
    category: frozen_success
    persona: backend
    phase: verify
    prompt: "..."
  assert:
    - type: is-json
    - type: contains
      value: "severity"
```

## Tolerance settings

`gate.py` enforces these invariants before opening a PR:

- **Frozen success set**: pass-rate must not drop by more than 5 percentage
  points compared to the pre-proposal baseline.
- **Target signature cases**: pass-rate must be >= 50% (configurable via
  `gate.py:GATE_CONFIG`).

These are conservative defaults. Raise the target threshold once the corpus
has enough cases for statistical significance (>= 10 per category).

## Running the eval manually

```bash
cd agents
promptfoo eval --config silphco/selfimprove/evals/promptfooconfig.yaml
```

Or via gate.py:

```bash
python -m silphco.selfimprove --dry-run
```

## Wiring to cron/systemd

See `docs/agents/self-improvement-loop.md` for the systemd unit and cron
examples.
