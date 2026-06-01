#!/usr/bin/env python3
"""Agent post-implement verification entrypoint.

Composes generic checks from agent_fleet.verify_core with silphco-specific
checks from agents.verify_checks, runs them via the fleet runner, and
exposes the same CLI contract as before:

    python verify.py <worktree-path>

Exits 0 on OK, 1 on RETRY or FATAL. Prints VerifyResult JSON to stdout.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, TypedDict

# Running as `python agents/verify.py` puts agents/agents/ on sys.path[0], which
# shadows stdlib logging via agents/agents/logging.py before agent_fleet imports.
_script_dir = Path(__file__).resolve().parent
_agents_root = _script_dir.parent
if sys.path and Path(sys.path[0]).resolve() == _script_dir:
    sys.path.pop(0)
if str(_agents_root) not in sys.path:
    sys.path.insert(0, str(_agents_root))

from agent_fleet.verify_core import (
    Check,
    CheckResult,
    check_no_debug_code,
    check_tests_for_modified_code,
    check_type_checking_ran,
    get_changed_files,
    run_checks,
)
from agents.verify_checks import (
    check_branch_sync,
    check_error_boundary_tests,
    check_integration_tests_for_chat_changes,
    check_no_agent_infrastructure_changes,
    check_no_verifier_self_modify,
    check_tool_coverage_on_removal,
    make_check_diff_respects_allowed_paths,
    make_check_no_secrets_leaked,
)

# Back-compat re-exports: external callers can still do
#   from verify import CheckResult, check_no_debug_code, ...
__all__ = [
    "Check",
    "CheckResult",
    "check_error_boundary_tests",
    "check_integration_tests_for_chat_changes",
    "check_no_agent_infrastructure_changes",
    "check_no_verifier_self_modify",
    "check_no_debug_code",
    "check_branch_sync",
    "check_tool_coverage_on_removal",
    "check_type_checking_ran",
    "get_changed_files",
    "make_check_diff_respects_allowed_paths",
    "make_check_no_secrets_leaked",
    "run_all_checks",
    "run_checks",
    "CHECKS",
]

# Silphco test discovery roots — passed through to the generic check.
_SILPHCO_TEST_ROOTS: tuple[str, ...] = (
    "tests",
    "api/tests",
    "frontend/src/__tests__",
)


def _tests_check(worktree_path: Path, files: list[str], issue_number: int) -> CheckResult:
    return check_tests_for_modified_code(
        worktree_path, files, issue_number, test_search_roots=_SILPHCO_TEST_ROOTS
    )


# Order matters: hard tripwires first so a FATAL aborts the rest.
CHECKS: tuple[Check, ...] = (
    check_no_verifier_self_modify,  # FATAL — stricter than infra tripwire; no fleet bypass
    check_no_agent_infrastructure_changes,  # FATAL — general protected-path tripwire
    check_branch_sync,  # rebase prompt before anything else
    _tests_check,
    check_integration_tests_for_chat_changes,
    check_error_boundary_tests,
    check_no_debug_code,
    check_tool_coverage_on_removal,
    check_type_checking_ran,
)


class _RunAllChecksResult(TypedDict):
    passed: bool
    severity: str
    checks: list[dict[str, Any]]
    violating_paths: list[str]
    files_changed: list[str]
    message: str


def run_all_checks(worktree_path: Path, issue_number: int) -> _RunAllChecksResult:
    """Back-compat shim for callers that used the old dict-returning API.

    Returns a dict with keys ``passed``, ``checks``, ``files_changed``, and
    ``message`` mirroring the old run_all_checks() shape so existing callers
    (e.g. agents.dispatch) don't need to change.
    """
    result = run_checks(worktree_path, CHECKS, issue_number)
    return {
        "passed": result.passed,
        "severity": result.severity.value,
        "checks": result.checks,
        "violating_paths": result.violating_paths,
        "files_changed": result.files_changed,
        "message": result.message,
    }


def main(*, issue_number: int = 0) -> int:
    """Run all verify checks against worktree.

    issue_number is required by the Check protocol but defaults to 0 for CLI
    usage where no GitHub issue context is available.
    """
    if len(sys.argv) < 2:
        print("Usage: verify-agent-work <worktree-path>", file=sys.stderr)
        return 1

    worktree_path = Path(sys.argv[1])
    if not worktree_path.exists():
        print(f"ERROR: Worktree path does not exist: {worktree_path}", file=sys.stderr)
        return 1

    if issue_number == 0:
        issue_number = int(os.environ.get("ISSUE_NUMBER", "0") or "0")

    result = run_checks(worktree_path, CHECKS, issue_number)
    print(json.dumps(result.to_dict(), indent=2))

    if not result.passed:  # passed is True iff severity is OK
        failed = [c for c in result.checks if not c["passed"]]
        marker = "🚨 TRIPWIRE" if result.severity.value == "fatal" else "❌"
        print(f"\n{marker} {len(failed)} check(s) failed:", file=sys.stderr)
        for c in failed:
            print(f"  - {c['name']}: {c['detail']}", file=sys.stderr)
        return 1

    print("\n✅ All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
