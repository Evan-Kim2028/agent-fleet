"""loop.py — Nightly self-improvement loop orchestrator.

Pipeline: mine → pick top signature → propose → guard-check → gate → open PR.

Safety constraints
------------------
* NEVER auto-merges.  All PRs are opened as drafts with an explicit human-review
  banner.  :class:`~fleet.hooks.GitForge` is injected — the loop never calls
  merge directly.
* Cap: at most 2 PRs per run, one file per PR.
* The gate must pass before any PR is opened.
* LLMBackend and GitForge are injectable for testing.

Exit codes (for cron/systemd)
------------------------------
* 0 — completed successfully (including "no actionable signature" no-op).
* 1 — unrecoverable error (gate or LLM call failed unexpectedly).
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agent_fleet.hooks import GitForge, LLMBackend
from silphco.selfimprove.gate import (
    GateEvalError,
    GatePreconditionError,
    GateResult,
    run_gate,
)
from silphco.selfimprove.guard import is_allowed
from silphco.selfimprove.mine import FailureSignature, SignatureBucket, mine
from silphco.selfimprove.propose import ChangeProposal, ProposerError, propose

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------

#: Maximum PRs to open in a single loop run.
MAX_PRS_PER_RUN: int = 2

#: Default minimum occurrence count to consider a signature actionable.
DEFAULT_MIN_OCCURRENCES: int = 5

#: Default look-back window for the mining step.
DEFAULT_DAYS: int = 30

#: Default log path (relative to repo root).
DEFAULT_LOG_PATH: str = "data/state/run_log.jsonl"

#: Branch prefix for self-improvement PRs.
BRANCH_PREFIX: str = "fleet/self-improve"

#: PR labels applied to all self-improvement PRs.
PR_LABELS: list[str] = ["self-improvement", "ai-proposed"]

#: Personas mapped to their prompt files (used to auto-select target_file).
PERSONA_PROMPT_FILES: dict[str, str] = {
    "backend": "agents/personas/backend.md",
    "frontend": "agents/personas/frontend.md",
    "data": "agents/personas/data.md",
    "pokemon_analyst": "agents/personas/pokemon_analyst.md",
    "security_qa": "agents/personas/security_qa.md",
}

#: Phase prompts live in the agent_fleet package (not editable repo paths):
#     plan       -> agent_fleet.planner
#     research   -> agent_fleet.researcher
#     synthesize -> agent_fleet.synthesizer
#     implement  -> agent_fleet.implementer
#     review     -> agent_fleet.reviewer
#     tech_lead  -> agent_fleet.tech_lead
PHASE_PROMPT_FILES: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LoopResult:
    """Summary of a single nightly loop run."""

    prs_opened: list[int]
    skipped_reason: str | None  # non-None when the loop no-oped
    proposals_attempted: int
    proposals_rejected: int


@dataclass
class _PRSpec:
    """Internal spec for a single PR to open."""

    signature: FailureSignature
    proposal: ChangeProposal
    gate_result: GateResult
    branch: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pick_target_file(signature: FailureSignature) -> str | None:
    """Determine the best target file for a given signature.

    Prefers the persona prompt file; falls back to the phase prompt file.
    Returns None when neither is permitted by the guard.
    """
    # Try persona file first
    persona_file = PERSONA_PROMPT_FILES.get(signature.persona)
    if persona_file and is_allowed(persona_file):
        return persona_file
    # Try phase file
    phase_file = PHASE_PROMPT_FILES.get(signature.phase)
    if phase_file and is_allowed(phase_file):
        return phase_file
    return None


def _branch_name(signature: FailureSignature, index: int) -> str:
    """Generate a short, unique branch name for a proposal."""
    error_slug = signature.error_class.value.replace("_", "-")
    return f"{BRANCH_PREFIX}/{signature.persona}/{signature.phase}/{error_slug}-{index}"


def _git_create_branch(branch: str, *, base: str = "main", cwd: Path) -> None:
    """Create and check out a new branch from *base* in *cwd*."""
    subprocess.run(
        ["git", "checkout", "-b", branch, base],
        cwd=str(cwd),
        check=True,
        capture_output=True,
    )


def _git_apply_diff(diff: str, *, cwd: Path, target_file: str) -> None:
    """Apply *diff* to the working tree in *cwd* using ``git apply``.

    Restricted to *target_file* via ``--include``/``--exclude='*'`` so that a
    manipulated diff header cannot write to any other path.  After applying, we
    verify that *only* target_file appears in the working-tree diff; any other
    modified path causes an immediate revert and raises.
    """
    import tempfile as _tmp
    with _tmp.NamedTemporaryFile(
        mode="w", suffix=".patch", delete=False, dir=str(cwd), encoding="utf-8"
    ) as f:
        f.write(diff)
        patch_path = f.name
    try:
        subprocess.run(
            [
                "git", "apply",
                "--whitespace=nowarn",
                f"--include={target_file}",
                "--exclude=*",
                patch_path,
            ],
            cwd=str(cwd),
            check=True,
            capture_output=True,
        )
    finally:
        Path(patch_path).unlink(missing_ok=True)

    # Defense-in-depth: confirm no stray files were written.
    result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )
    modified = [p for p in result.stdout.splitlines() if p]
    unexpected = [p for p in modified if p != target_file]
    if unexpected:
        # Revert ALL working-tree changes to leave the repo clean.
        subprocess.run(
            ["git", "checkout", "--", "."],
            cwd=str(cwd),
            capture_output=True,
        )
        raise RuntimeError(
            f"_git_apply_diff: patch modified unexpected files {unexpected!r}; "
            f"expected only {target_file!r}. Reverted."
        )


def _git_commit(message: str, *, cwd: Path, files: list[str]) -> None:
    """Stage *files* and commit with *message*."""
    subprocess.run(
        ["git", "add", "--"] + files,
        cwd=str(cwd),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(cwd),
        check=True,
        capture_output=True,
    )


def _build_pr_body(
    spec: _PRSpec,
    bucket: SignatureBucket,
) -> str:
    """Build a PR body with signature, eval scores, trace exemplars, and safety banner."""
    sig = spec.signature
    gate = spec.gate_result
    proposal = spec.proposal

    trace_lines: list[str] = []
    for t in bucket.traces[:3]:
        detail = (t.detail or "")[:300]
        trace_lines.append(
            f"- `{t.ts}` | run={t.run_id} | issue={t.issue} | "
            f"duration={t.duration_s}s\n  `{detail}`"
        )
    traces_section = "\n".join(trace_lines) or "_No traces available._"

    frozen_drop = gate.frozen_before.pass_rate - gate.frozen_after.pass_rate
    return f"""\
> **AI-PROPOSED — HUMAN REVIEW REQUIRED**
> This PR was opened automatically by the nightly self-improvement loop.
> It must NOT be merged without human review and explicit approval.

## Failure signature

| Field | Value |
|---|---|
| Persona | `{sig.persona}` |
| Phase | `{sig.phase}` |
| Error class | `{sig.error_class.value}` |
| Occurrences (window) | {bucket.count} |
| Total cost | {bucket.total_cost:.1f}s |

## Root-cause hypothesis

{proposal.rationale}

## Eval scores (promptfoo gate)

| Metric | Value |
|---|---|
| Frozen-success (before) | {gate.frozen_before.pass_rate:.1%} ({gate.frozen_before.passed}/{gate.frozen_before.total}) |
| Frozen-success (after) | {gate.frozen_after.pass_rate:.1%} ({gate.frozen_after.passed}/{gate.frozen_after.total}) |
| Frozen-success drop | {frozen_drop:.1%} |
| Target-signature (after) | {gate.target_after.pass_rate:.1%} ({gate.target_after.passed}/{gate.target_after.total}) |

## Sample failure traces

{traces_section}

## Target file

`{proposal.target_file}`

## Proposed diff

```diff
{proposal.diff}
```

---
_Opened by `silphco.selfimprove.loop` — do not auto-merge._
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_loop(
    *,
    repo_root: Path,
    backend: LLMBackend,
    forge: GitForge,
    log_path: Path | None = None,
    days: int = DEFAULT_DAYS,
    min_occurrences: int = DEFAULT_MIN_OCCURRENCES,
    base_branch: str = "main",
    max_prs: int = MAX_PRS_PER_RUN,
    dry_run: bool = False,
) -> LoopResult:
    """Execute one nightly self-improvement pass.

    Args:
        repo_root: Absolute path to the repository root (used for file I/O
            and git commands).
        backend: LLM backend for the propose step.
        forge: GitForge adapter for opening PRs.
        log_path: Path to the run log.  Defaults to
            ``repo_root / DEFAULT_LOG_PATH``.
        days: Look-back window for the mining step.
        min_occurrences: Minimum occurrence count to be actionable.
        base_branch: Git branch to branch from for each PR.
        max_prs: Maximum PRs to open in this run.
        dry_run: When True, skip PR creation and git operations (useful for
            testing gate logic without side effects).

    Returns:
        :class:`LoopResult` summarising the run.
    """
    if log_path is None:
        log_path = repo_root / DEFAULT_LOG_PATH

    # --- Mine ---
    log.info("Mining failure signatures from %s (last %d days)", log_path, days)
    ranked = mine(log_path, days=days, min_occurrences=min_occurrences)

    if not ranked:
        log.info("No actionable signatures above threshold=%d — no-op.", min_occurrences)
        return LoopResult(
            prs_opened=[],
            skipped_reason=f"No signature meets min_occurrences={min_occurrences}.",
            proposals_attempted=0,
            proposals_rejected=0,
        )

    log.info("Found %d actionable signatures; processing up to %d.", len(ranked), max_prs)

    prs_opened: list[int] = []
    proposals_attempted = 0
    proposals_rejected = 0
    pr_index = 0

    for bucket in ranked:
        if len(prs_opened) >= max_prs:
            break

        sig = bucket.signature
        log.info(
            "Processing signature: persona=%s phase=%s error_class=%s count=%d",
            sig.persona, sig.phase, sig.error_class.value, bucket.count,
        )

        # Determine target file
        target_file = _pick_target_file(sig)
        if target_file is None:
            log.warning(
                "No allowed target file for %s/%s — skipping.", sig.persona, sig.phase
            )
            proposals_rejected += 1
            continue

        # --- Propose ---
        proposals_attempted += 1
        try:
            proposal = propose(
                sig,
                bucket.traces,
                target_file,
                backend=backend,
                repo_root=repo_root,
            )
        except ProposerError as exc:
            log.warning("Proposer rejected: %s", exc)
            proposals_rejected += 1
            continue

        # --- Gate ---
        log.info("Running regression gate for %s", target_file)
        try:
            gate_result = run_gate(proposal, repo_root=repo_root)
        except GatePreconditionError as exc:
            log.error("Gate precondition error (promptfoo missing?): %s", exc)
            raise
        except GateEvalError as exc:
            log.warning("Gate eval error: %s", exc)
            proposals_rejected += 1
            continue

        if not gate_result.passed:
            log.info("Gate rejected proposal: %s", gate_result.reason)
            proposals_rejected += 1
            continue

        log.info("Gate passed: %s", gate_result.reason)

        if dry_run:
            log.info("[dry-run] Would open PR for %s — skipping git/forge ops.", target_file)
            prs_opened.append(-1)  # sentinel for tests
            pr_index += 1
            continue

        # --- Create branch + apply diff + commit ---
        branch = _branch_name(sig, pr_index)
        pr_index += 1

        try:
            _git_create_branch(branch, base=base_branch, cwd=repo_root)
            _git_apply_diff(proposal.diff, cwd=repo_root, target_file=target_file)
            commit_msg = (
                f"self-improve({sig.persona}/{sig.phase}): "
                f"{sig.error_class.value} — AI-proposed, human review required\n\n"
                f"Failure signature: count={bucket.count} score={bucket.score:.1f}\n"
                f"Target: {target_file}"
            )
            _git_commit(commit_msg, cwd=repo_root, files=[target_file])
        except subprocess.CalledProcessError as exc:
            log.error("Git operation failed: %s", exc)
            proposals_rejected += 1
            # Try to clean up by checking out base branch
            subprocess.run(
                ["git", "checkout", base_branch],
                cwd=str(repo_root),
                capture_output=True,
            )
            continue

        # --- Open PR (NEVER auto-merge) ---
        pr_body = _build_pr_body(
            _PRSpec(
                signature=sig,
                proposal=proposal,
                gate_result=gate_result,
                branch=branch,
            ),
            bucket,
        )
        try:
            pr_number = forge.open_pr(
                title=(
                    f"[self-improve] {sig.persona}/{sig.phase}: "
                    f"{sig.error_class.value}"
                ),
                body=pr_body,
                branch=branch,
                base=base_branch,
                draft=True,  # ALWAYS draft — never auto-merge
                labels=PR_LABELS,
            )
        except Exception as exc:  # noqa: BLE001 — broad catch; PR open is best-effort
            log.error("Failed to open PR: %s", exc)
            proposals_rejected += 1
            # Check back out to base branch to leave repo in clean state
            subprocess.run(
                ["git", "checkout", base_branch],
                cwd=str(repo_root),
                capture_output=True,
            )
            continue

        log.info("Opened PR #%d for %s", pr_number, target_file)
        prs_opened.append(pr_number)

        # Return to base branch for next iteration
        subprocess.run(
            ["git", "checkout", base_branch],
            cwd=str(repo_root),
            capture_output=True,
        )

    return LoopResult(
        prs_opened=prs_opened,
        skipped_reason=None,
        proposals_attempted=proposals_attempted,
        proposals_rejected=proposals_rejected,
    )
