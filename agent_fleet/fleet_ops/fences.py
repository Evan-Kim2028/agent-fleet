"""Standing fences carried into every implementer prompt.

The bash drivers appended a shared ``prompts/fences.md`` to every
implementation prompt (``fbrun``'s ``===== STANDING FENCES =====`` block). The
fences are not a suggestion about this particular task — they encode the
house rules that hold across every lane, and an agent that only ever reads its
task file will happily walk into one.

They live in code, not in an operator's prompt directory, because a fence that
can go missing when a scratchpad is cleaned is not a fence. A repo may *extend*
them (``FleetOpsConfig.fences``) but never shorten them: a task instruction does
not override a fence unless it names the owner approval explicitly.
"""

from __future__ import annotations

#: The fence block, appended verbatim to every implementer prompt.
DEFAULT_FENCES: tuple[str, ...] = (
    "Other sessions are active on these repos. Files listed as fenced are owned by "
    "another lane: do not edit them. If your task seems to require editing one, stop "
    "and report it instead of working around it.",
    "Never git stash, git reset --hard, git checkout --, or git clean. Other lanes' "
    "work shares this machine.",
    "Never `git commit --no-verify`, and never disable hooks. If a hook is red on the "
    "base branch for reasons outside your diff, skip only that named hook with "
    "SKIP=<hook-id> and let every other hook run.",
    "Never edit .github/workflows/*, deploy scripts, or install/enable systemd units, "
    "timers, or crontabs. Ops automation belongs to other sessions: document the "
    "needed operator step in the PR body instead.",
    "Never run, and never recommend in a PR body, any gold.sales restatement or "
    "backfill for sale_date before 2026-08-01. A day-level sales rebuild deletes the "
    "whole day for all sources and re-inserts only the per-venue models, which start "
    "2026-08-01, so it wipes history. For a stamp or grade rule change, the PR body "
    'must say: "documents-26 applies this via an in-place stamp-column update of the '
    'affected gold rows; no restatement command." You may list the affected sale_ids '
    "and counts (bounded, read-only).",
    "Never write gold.sales or any table on prod. Never run ad-hoc production dbt or "
    "Dagster jobs, and never run production queries between 08:30 and 12:00 UTC.",
    "Run every test suite under a memory cap (systemd-run --user --scope "
    "-p MemoryMax=6G, or ulimit -v 6000000). If a test needs more, stop and report "
    "rather than raising the cap.",
    "Run only targeted test files for the modules you changed. Never run a whole "
    "repository or whole-pipeline suite.",
    "Manifest outputs are never auto-allowlisted; a family goes on any allowlist only "
    "by an operator's hand.",
    "Check IO pressure before starting heavy work and back off above 10.",
)

#: Header the fences are written under, matching the bash drivers' block.
FENCES_HEADER = "===== STANDING FENCES (always apply) ====="


def render_fences(extra: tuple[str, ...] = ()) -> str:
    """Render the fence block: every default fence, then any repo extensions.

    *extra* appends; it never replaces. A repo can add a rule that is specific to
    its own data, but the house rules above always ship.
    """
    lines = [f"- {fence}" for fence in DEFAULT_FENCES]
    lines.extend(f"- {fence}" for fence in extra if fence.strip())
    return "\n".join([FENCES_HEADER, *lines])


__all__ = ["DEFAULT_FENCES", "FENCES_HEADER", "render_fences"]
