## What this ports

Token-efficient reviewer brief in the Python `agent_fleet/gate/` package, from the bash gate running in production (`~/fleet/fb/fbgate`).

A lens reviewer told only to run `git diff` goes and gets it — measured at **~44 model calls per reviewer**, each re-uploading a context it has already grown. On the shared uplink that is the gate's binding cost, not the review's.

## Changes

**The change goes inline.** Diffed **once per run** and embedded in every reviewer prompt: 25 lines of context, tests and markdown excluded. The prompt says review *this*, do not re-run `git diff`, and gives the tool budget ("aim for at most ~30 tool calls") plus the one legitimate reason to open a file — confirming or rejecting a specific suspected blocker, never browsing.

**Truncation is explicit.** Capped at `gate.diff_chars` (150000). A cap that cuts the diff says `(TRUNCATED at N chars: run git diff for the rest)` in the prompt, because the alternative is worse than useless: a silently clipped diff reads as the complete change and the dropped half is never reviewed.

**The base is the PR's own base branch**, taken from the forge, with `gate.base_branch` as fallback. A stacked PR diffed against `main` shows reviewers the base branch's commits as its own and sizes the PR by work it never did. Resolved once, used at **every** site the gate diffs: step0, reviewer prompts, verifiers, judge, the patch-id carry-over, and the merged-tree regression check — which now merges `origin/<base>` so the deterministic half runs against the tree the PR will actually produce.

An explicit `origin/<branch>`, `refs/...`, or a sha in the config is the operator speaking directly and is never overridden.

**Turn caps are configurable per stage**: `review_turns` (60), `fix_turns` (120). A wall-clock budget cannot stop an agent working badly for its whole budget, and every turn re-uploads the context it grew. The cap reaches `cmd` and `grok` as `--max-turns`; the other backends accept and ignore the keyword, so setting it can never become a `TypeError` that kills a stage. A reviewer that hits the cap without a verdict stays **dead evidence**, exactly like a crash — the gate still fails closed.

## Notes for the reviewer

- **Exclusion pathspecs are subtle and worth a look.** A bare `:(exclude)*.md` matches at *any* depth, and adding `glob` to that same pattern *stops* it matching at depth — `:(exclude,glob)*.md` silently leaves every nested markdown in. A directory also needs a trailing `/**`: a bare `:(exclude)tests/` never matches `api/tests`. I verified each form against a real repo rather than reasoning about it; `tests/test_gate_reviewer_brief.py::test_inline_change_excludes_nested_tests_and_markdown` pins it.
- **The recheck now makes one extra `gh pr view` call** to learn the base branch. It is wrapped so an unreachable forge falls back to the configured branch instead of failing the check — a recheck that cannot establish a verdict is a refusal, not a network error.
- **No gold.sales restatement or backfill** is involved or recommended: this touches no gold table and no stamp or grade rule.

## Size

~250 lines of non-test code, plus docs. One concern.

## Tests

`tests/test_gate_reviewer_brief.py` (23 tests) — real git repos in tmp_path, stubbed agent backends, no network and no model calls.

```
uv run --group dev pytest -q tests/test_gate_reviewer_brief.py tests/test_gate_*.py -x
# 455 passed
```

Also run, since the backends changed: `tests/test_cmd_backend.py tests/test_grok_backend.py tests/test_devin_backend.py tests/test_openrouter_backend.py tests/test_backend_registry.py` — 262 passed, 2 skipped. `ruff format`, `ruff check` and `ty check` clean on every file in the diff.

The one `E501` in `agent_fleet/serve/__init__.py` is pre-existing baseline debt in a file this diff does not touch.

All test runs were under `systemd-run --user --scope -p MemoryMax=6G`.
