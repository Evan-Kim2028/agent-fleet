"""Gate invocation — a feature-detected seam, not an implementation.

The ``gate`` pipeline (find → verify-with-a-failing-test → one fix → recheck) is
being built in a **parallel lane** (``fb/fleetgate``) and is not part of this PR.
This module therefore does exactly one thing: find out whether a gate is
available, and if so hand the PR to it.

The seam is deliberately thin and the skip is a *first-class outcome*, not an
error. Until ``agent-fleet gate`` lands, ``lane run`` still does its most
important job — guaranteeing the PR — and reports the lane as ``pr_guaranteed``
with a ``GATE-SKIPPED`` event. When the gate merges, the same call site starts
working with no change here, because detection is by capability (does the
subcommand exist?) rather than by version.

**Nothing runs until the binding is verified.** :func:`run_gate` takes a
:class:`~agent_fleet.fleet_ops.binding.LaneBinding`, which can only be produced
by a check that the PR's ``headRefName`` really is this lane's branch and that
the repo came from the worktree's own ``origin`` remote. That is what stops a
stray ``REVIEW_REPO`` from sending four review lenses at another team's PR.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agent_fleet.fleet_ops.binding import LaneBinding

    Runner = Callable[..., "subprocess.CompletedProcess[str]"]

logger = logging.getLogger(__name__)

#: The gate's own approval marker, matching the status-line contract the bash
#: automerge already consumed.
APPROVAL_MARKER = "PREMERGE-APPROVED"

#: Markers that also mean "approved", weaker than the exact token above.
APPROVAL_FALLBACK_MARKERS = ("APPROVE",)

#: The escalation line the gate emits when it could not clear the PR.
ESCALATION_MARKER = "NEEDS-ESCALATION"

#: Other ways the gate can decline, checked on the last line before any approval
#: marker is considered.
REJECTION_MARKERS = ("REJECT", "NEEDS-FIX", "FAIL")

#: Subcommand name looked for on the console script.
GATE_SUBCOMMAND = "gate"


@dataclass(frozen=True)
class GateOutcome:
    """What happened when we handed a PR to the gate."""

    available: bool
    approved: bool = False
    reason: str = ""
    sha9: str | None = None
    output: str = ""
    exit_code: int | None = None
    #: True once the gate was actually invoked. Distinguishes "the gate ran and
    #: declined" from "the gate was never there".
    ran: bool = False

    @property
    def skipped(self) -> bool:
        return not self.available

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "approved": self.approved,
            "reason": self.reason,
            "sha9": self.sha9,
            "exit_code": self.exit_code,
            "ran": self.ran,
        }


def gate_available(
    known_subcommands: set[str] | str | None = None,
    *,
    runner: Runner | None = None,
) -> bool:
    """Whether an ``agent-fleet gate`` entry point exists.

    *known_subcommands* may be passed explicitly (the CLI has ``sub.choices`` in
    hand, which is the cheapest and most accurate check); when omitted, detection
    falls back to probing ``agent-fleet gate --help``. A missing binary, a
    non-zero exit, or output that does not mention ``gate`` all mean "not
    available" — never an exception.
    """
    if known_subcommands is not None:
        if isinstance(known_subcommands, str):
            return known_subcommands.strip() == GATE_SUBCOMMAND
        return GATE_SUBCOMMAND in known_subcommands

    run = runner or subprocess.run
    try:
        result = run(
            ["agent-fleet", GATE_SUBCOMMAND, "--help"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except FileNotFoundError, OSError, subprocess.SubprocessError:
        return False
    if result.returncode != 0:
        return False
    return GATE_SUBCOMMAND in f"{result.stdout or ''}\n{result.stderr or ''}".lower()


def _last_meaningful_line(output: str) -> str:
    for line in reversed((output or "").splitlines()):
        if line.strip():
            return line.strip()
    return ""


def _classify(output: str) -> tuple[bool, str, str]:
    """Decide ``(approved, reason, approval_line)`` from the gate's *last* line.

    Reading the last meaningful line rather than the whole transcript is the
    point. A gate that runs several lenses ends with a summary, and scanning all
    of it is how ``NEEDS-ESCALATION: did not APPROVE the fix`` gets read as an
    approval — the most dangerous false positive in this module, because it lets
    an unapproved lane reach the merge path.
    """
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    last = lines[-1] if lines else ""

    if ESCALATION_MARKER in last or any(m in last for m in REJECTION_MARKERS):
        return False, last or "gate did not approve", ""

    approval_line = next((line for line in reversed(lines) if APPROVAL_MARKER in line), "")
    if approval_line:
        return True, "gate approved", approval_line
    if any(marker in last for marker in APPROVAL_FALLBACK_MARKERS):
        return True, "gate approved", last
    return False, last or "gate produced no approval line", ""


def _sha9_from_line(line: str) -> str | None:
    """Pull the sha9 off an approval line, or None if the tail is not a sha.

    The bash drivers guarded this the same way (``[ ${#sha} -ge 7 ]``): a line
    ending in prose must not have its last word mistaken for a commit id.
    """
    parts = line.split()
    if not parts:
        return None
    tail = parts[-1]
    if len(tail) < 7 or not all(c in "0123456789abcdef" for c in tail.lower()):
        return None
    return tail


def build_gate_args(
    binding: LaneBinding, *, lane: str, judge_engine: str | None = None
) -> list[str]:
    """The ``agent-fleet gate`` argv for an already-verified binding.

    ``--repo`` carries the *verified* slug from the worktree's origin, so the
    gate never has to infer which repository it is judging. This is redundant
    with the binding check on purpose: the cost of resolving to the wrong repo is
    a whole review pipeline pointed at the wrong code.
    """
    args = [
        "agent-fleet",
        GATE_SUBCOMMAND,
        "--lane",
        lane,
        "--repo",
        binding.repo_slug,
        "--pr",
        str(binding.pr),
        "--head-ref",
        binding.branch,
    ]
    if judge_engine:
        args += ["--judge-engine", judge_engine]
    return args


def run_gate(
    *,
    lane: str,
    binding: LaneBinding,
    cwd: Path,
    judge_engine: str | None = None,
    known_subcommands: set[str] | None = None,
    runner: Runner | None = None,
    env: dict[str, str] | None = None,
    timeout_s: int = 4 * 3600,
) -> GateOutcome:
    """Hand a *verified* PR to the gate, if a gate exists.

    A missing gate is not an error: the lane keeps its guaranteed PR and the
    caller records ``GATE-SKIPPED``. Approval is read from the gate's own status
    line (``PREMERGE-APPROVED <sha9>``), the same contract the bash automerge
    consumed, so a gate written to that contract needs no adapter here.
    """
    if not gate_available(known_subcommands):
        return GateOutcome(
            available=False,
            reason=(
                "no `agent-fleet gate` entry point found (gate pipeline not merged); "
                "PR is guaranteed but ungated"
            ),
        )

    args = build_gate_args(binding, lane=lane, judge_engine=judge_engine)
    run = runner or subprocess.run
    try:
        result = run(
            args,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
        return GateOutcome(available=True, ran=True, reason=f"gate invocation failed: {exc}")

    output = f"{result.stdout or ''}\n{result.stderr or ''}"
    approved, reason, approval_line = _classify(output)
    if not approved and result.returncode not in (0, None) and not reason:
        reason = f"gate exited {result.returncode}"

    return GateOutcome(
        available=True,
        ran=True,
        approved=approved,
        reason=reason,
        sha9=(_sha9_from_line(approval_line) or binding.head_sha[:9] or None) if approved else None,
        output=output,
        exit_code=result.returncode,
    )


__all__ = [
    "APPROVAL_MARKER",
    "ESCALATION_MARKER",
    "GATE_SUBCOMMAND",
    "GateOutcome",
    "build_gate_args",
    "gate_available",
    "run_gate",
]
