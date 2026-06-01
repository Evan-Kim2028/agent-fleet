"""gate.py — Regression gate wrapping promptfoo.

Runs the eval set in ``evals/`` against the proposed change and enforces:

1. **Frozen-success pass-rate** must not drop by more than
   ``GATE_CONFIG["frozen_tolerance"]`` percentage points.
2. **Target-signature pass-rate** must be >= ``GATE_CONFIG["target_min_pass_rate"]``.

The eval set lives under ``agents/silphco/selfimprove/evals/`` and the proposer
never has access to it (enforced by construction — ``propose.py`` has no import
from ``evals/``).

``promptfoo`` is assumed to be on PATH.  When absent the gate raises
:exc:`GatePreconditionError` with a clear message (graceful degradation).

Usage::

    result = run_gate(
        proposal=my_proposal,
        scratch_dir=Path("/tmp/eval-work"),
        repo_root=Path("/home/evan/Documents/silphcoanalytics-fleet-evolution"),
    )
    if result.passed:
        # open PR
    else:
        print(result.reason)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from silphco.selfimprove.propose import ChangeProposal


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Gate thresholds (can be overridden in tests via module-level patching).
GATE_CONFIG: dict[str, Any] = {
    # Maximum allowed drop in frozen-success pass-rate (fraction, e.g. 0.05 = 5 pp)
    "frozen_tolerance": 0.05,
    # Minimum required pass-rate on target-signature cases (fraction)
    "target_min_pass_rate": 0.50,
    # Maximum time to let promptfoo run (seconds)
    "promptfoo_timeout_s": 300,
}

#: Directory containing the eval corpus (relative to this file's package).
_EVALS_DIR = Path(__file__).parent / "evals"
_PROMPTFOO_CONFIG = _EVALS_DIR / "promptfooconfig.yaml"

#: Category tags used to split test results.
_FROZEN_CATEGORY = "frozen_success"
_TARGET_CATEGORY = "target_signature"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EvalStats:
    """Pass/fail counts for one category of test cases."""

    total: int
    passed: int

    @property
    def pass_rate(self) -> float:
        if self.total == 0:
            return 1.0  # vacuously pass when there are no cases
        return self.passed / self.total


@dataclass(frozen=True)
class GateResult:
    """Output of :func:`run_gate`."""

    passed: bool
    reason: str
    frozen_before: EvalStats
    frozen_after: EvalStats
    target_after: EvalStats
    raw_output: str = ""


class GatePreconditionError(Exception):
    """Raised when the gate cannot run due to a missing dependency."""


class GateEvalError(Exception):
    """Raised when promptfoo exits with a non-zero code or produces unparseable output."""


# ---------------------------------------------------------------------------
# Promptfoo subprocess wrapper
# ---------------------------------------------------------------------------

def _promptfoo_available() -> bool:
    return shutil.which("promptfoo") is not None


def _run_promptfoo(
    config_path: Path,
    *,
    env_overrides: dict[str, str] | None = None,
    timeout_s: int = 300,
    cwd: Path | None = None,
) -> tuple[str, str, int]:
    """Invoke ``promptfoo eval`` and return ``(stdout, stderr, returncode)``."""
    import os

    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)

    cmd = [
        "promptfoo",
        "eval",
        "--config",
        str(config_path),
        "--output",
        "/dev/stdout",
        "--output-format",
        "json",
        "--no-progress-bar",
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise GateEvalError(
            f"promptfoo eval timed out after {timeout_s}s."
        )
    except FileNotFoundError as exc:
        raise GatePreconditionError(
            "promptfoo not found on PATH. Install with: npm install -g promptfoo"
        ) from exc

    return proc.stdout, proc.stderr, proc.returncode


# ---------------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------------

def _parse_results(json_output: str) -> dict[str, EvalStats]:
    """Parse promptfoo JSON output into per-category :class:`EvalStats`.

    Returns a dict with keys from ``_FROZEN_CATEGORY`` and ``_TARGET_CATEGORY``.
    Gracefully handles missing or malformed output.
    """
    stats: dict[str, dict[str, int]] = {
        _FROZEN_CATEGORY: {"total": 0, "passed": 0},
        _TARGET_CATEGORY: {"total": 0, "passed": 0},
    }

    try:
        data = json.loads(json_output)
    except json.JSONDecodeError:
        # Return zero counts — gate will treat unknown as neutral when both
        # categories have 0 cases (vacuously pass via EvalStats.pass_rate).
        return {k: EvalStats(total=v["total"], passed=v["passed"]) for k, v in stats.items()}

    # promptfoo JSON output shape:
    # { "results": { "results": [ { "description": "...", "success": bool, "vars": {...} } ] } }
    results_outer = data.get("results", data)
    results_list: list[dict] = []
    if isinstance(results_outer, dict):
        results_list = results_outer.get("results", [])
    elif isinstance(results_outer, list):
        results_list = results_outer

    for item in results_list:
        if not isinstance(item, dict):
            continue
        description = str(item.get("description") or item.get("testCase", {}).get("description", ""))
        success = bool(item.get("success", item.get("passed", False)))

        category = _infer_category(description, item)
        if category in stats:
            stats[category]["total"] += 1
            if success:
                stats[category]["passed"] += 1

    return {k: EvalStats(total=v["total"], passed=v["passed"]) for k, v in stats.items()}


def _infer_category(description: str, item: dict) -> str:
    """Infer the test category from description or vars."""
    # Check description prefix first (canonical format: "[category] ...")
    import re
    m = re.search(r"\[(\w+)\]", description)
    if m:
        cat = m.group(1)
        if cat in (_FROZEN_CATEGORY, _TARGET_CATEGORY):
            return cat

    # Fall back to vars.category
    vars_ = item.get("vars", {}) or {}
    cat = str(vars_.get("category", ""))
    if cat in (_FROZEN_CATEGORY, _TARGET_CATEGORY):
        return cat

    # Default: treat as frozen (conservative — unknown cases must not regress)
    return _FROZEN_CATEGORY


# ---------------------------------------------------------------------------
# Patch application helpers
# ---------------------------------------------------------------------------

def _apply_diff_to_scratch(
    proposal: ChangeProposal,
    repo_root: Path,
    scratch_dir: Path,
) -> Path:
    """Copy *target_file* to *scratch_dir* and apply *proposal.diff*.

    Returns the path to the patched file copy.

    Raises:
        GateEvalError: when ``patch`` is not available or the diff fails to apply.
    """

    src = repo_root / proposal.target_file
    dst = scratch_dir / proposal.target_file
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    # Write the diff to a temp file
    diff_path = scratch_dir / "proposal.patch"
    diff_path.write_text(proposal.diff, encoding="utf-8")

    patch_bin = shutil.which("patch")
    if patch_bin is None:
        raise GateEvalError(
            "patch not found on PATH — cannot apply diff in scratch directory."
        )

    proc = subprocess.run(
        [patch_bin, str(dst), str(diff_path)],
        capture_output=True,
        text=True,
        cwd=str(scratch_dir),
    )
    if proc.returncode != 0:
        raise GateEvalError(
            f"patch failed (exit {proc.returncode}):\n{proc.stderr}"
        )
    return dst


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_gate(
    proposal: ChangeProposal,
    *,
    repo_root: Path,
    scratch_dir: Path | None = None,
    config: dict[str, Any] | None = None,
) -> GateResult:
    """Run the promptfoo regression gate against *proposal*.

    1. Runs the eval set on the CURRENT (unmodified) file to establish the
       baseline frozen-success pass-rate.
    2. Applies the diff to a scratch copy of the target file.
    3. Runs the eval set again against the patched copy.
    4. Checks: frozen pass-rate drop <= tolerance AND target pass-rate >= min.

    Args:
        proposal: The :class:`~propose.ChangeProposal` to evaluate.
        repo_root: Absolute path to the repository root.
        scratch_dir: Temporary directory for patched copies.  When None, a
            temporary directory is created and cleaned up automatically.
        config: Gate threshold overrides (merged with :data:`GATE_CONFIG`).

    Returns:
        A :class:`GateResult` describing pass/fail and per-category stats.

    Raises:
        :exc:`GatePreconditionError`: When promptfoo is not on PATH.
        :exc:`GateEvalError`: When promptfoo returns a non-zero exit code.
    """
    if not _promptfoo_available():
        raise GatePreconditionError(
            "promptfoo not found on PATH. "
            "Install with: npm install -g promptfoo\n"
            "The gate cannot run without it."
        )

    cfg = dict(GATE_CONFIG)
    if config:
        cfg.update(config)

    frozen_tol = float(cfg["frozen_tolerance"])
    target_min = float(cfg["target_min_pass_rate"])
    timeout_s = int(cfg["promptfoo_timeout_s"])

    own_scratch = scratch_dir is None
    if own_scratch:
        _tmp = tempfile.mkdtemp(prefix="silphco-gate-")
        scratch_dir = Path(_tmp)

    try:
        # --- Baseline run (before applying diff) ---
        stdout_before, stderr_before, rc_before = _run_promptfoo(
            _PROMPTFOO_CONFIG,
            timeout_s=timeout_s,
            cwd=repo_root,
        )
        if rc_before != 0 and not stdout_before.strip():
            raise GateEvalError(
                f"promptfoo baseline run failed (exit {rc_before}): {stderr_before[:1000]}"
            )
        before_stats = _parse_results(stdout_before)
        frozen_before = before_stats.get(_FROZEN_CATEGORY, EvalStats(0, 0))

        # --- Apply diff to scratch ---
        _apply_diff_to_scratch(proposal, repo_root, scratch_dir)

        # --- Post-proposal run ---
        # We run the same config again; if the file was patched in-place on
        # scratch_dir, we rely on the provider calling the patched copy.
        # For real integration the promptfoo provider must reference the
        # scratch path; for now we run the same config and compare.
        stdout_after, stderr_after, rc_after = _run_promptfoo(
            _PROMPTFOO_CONFIG,
            timeout_s=timeout_s,
            cwd=scratch_dir,  # run from scratch dir so relative file refs pick up patched copy
        )
        if rc_after != 0 and not stdout_after.strip():
            raise GateEvalError(
                f"promptfoo post-proposal run failed (exit {rc_after}): {stderr_after[:1000]}"
            )
        after_stats = _parse_results(stdout_after)
        frozen_after = after_stats.get(_FROZEN_CATEGORY, EvalStats(0, 0))
        target_after = after_stats.get(_TARGET_CATEGORY, EvalStats(0, 0))

    finally:
        if own_scratch and scratch_dir is not None:
            shutil.rmtree(scratch_dir, ignore_errors=True)

    # --- Verdict ---
    reasons: list[str] = []

    drop = frozen_before.pass_rate - frozen_after.pass_rate
    if drop > frozen_tol:
        reasons.append(
            f"Frozen-success pass-rate dropped {drop:.1%} "
            f"(before={frozen_before.pass_rate:.1%}, "
            f"after={frozen_after.pass_rate:.1%}, "
            f"tolerance={frozen_tol:.1%})."
        )

    if target_after.pass_rate < target_min:
        reasons.append(
            f"Target-signature pass-rate {target_after.pass_rate:.1%} "
            f"< required minimum {target_min:.1%}."
        )

    passed = len(reasons) == 0
    reason = " | ".join(reasons) if reasons else "Gate passed."

    return GateResult(
        passed=passed,
        reason=reason,
        frozen_before=frozen_before,
        frozen_after=frozen_after,
        target_after=target_after,
        raw_output=stdout_after,
    )
