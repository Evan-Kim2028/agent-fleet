"""Tests for verify-failure classification and the fix-loop bootstrap guard (E1).

A broken test harness (import/collection/startup crash) is not fixable by another
rewrite, so the fix loop must surface it instead of spending full-context fix
agents against it. A genuine assertion failure ran the harness and stays fixable.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agent_fleet.code_review.config import CodeReviewConfig
from agent_fleet.phases import classify_verify_failure, last_verify_failure_is_bootstrap

# The verbatim stderr from the 3.9M-token MED reshape baseline (dispatch-0-51f59f56):
# pytest crashed in _prepareconfig before any test ran.
_BASELINE_STARTUP_CRASH = """Traceback (most recent call last):
  File "/tmp/agent-fleet-worktrees/task-0-2b9fa11d/.venv/bin/pytest", line 10, in <module>
    sys.exit(console_main())
  File ".../_pytest/config/__init__.py", line 223, in console_main
    code = main()
  File ".../_pytest/config/__init__.py", line 193, in main
    config = _prepareconfig(args, plugins)
ModuleNotFoundError: No module named 'lakestore.raw_lane'
"""

_COLLECTION_ERROR = """==== ERRORS ====
___ ERROR collecting packages/lakestore/tests/test_lane_isolation.py ___
ImportError while importing test module 'test_lane_isolation.py'.
ModuleNotFoundError: No module named 'lakestore.raw_lane'
collected 0 items
!!! Interrupted: 1 error during collection !!!
"""

_SYNTAX_ERROR = """==== ERRORS ====
___ ERROR collecting packages/lakestore/tests/test_x.py ___
.../test_x.py:5: in <module>
    def broken(:
SyntaxError: invalid syntax
!!! Interrupted: 1 error during collection !!!
"""

_ASSERTION_FAILURE = """collected 3 items
packages/lakestore/tests/test_lane_isolation.py F
==== FAILURES ====
___ test_tx_origin_absent ___
    assert 'tx_origin' not in SOLANA_RAW_SCHEMA
AssertionError
==== short test summary info ====
FAILED packages/lakestore/tests/test_lane_isolation.py::test_tx_origin_absent
1 failed in 0.12s
"""

# A test that itself raises ModuleNotFoundError at runtime: the harness DID run,
# so this is a real, fixable failure, not a collection crash.
_IN_TEST_IMPORT_ERROR = """collected 5 items
packages/lakestore/tests/test_x.py F
==== FAILURES ====
___ test_optional_dep ___
    import lakestore.raw_lane
ModuleNotFoundError: No module named 'lakestore.raw_lane'
==== short test summary info ====
FAILED packages/lakestore/tests/test_x.py::test_optional_dep
1 failed in 0.20s
"""

_LINT_FAILURE = """packages/lakestore/src/x.py:3:1: F401 [*] `os` imported but unused
Found 1 error.
"""


def _verify(detail: str, *, passed: bool = False) -> dict[str, object]:
    return {"phase": "verify", "passed": passed, "exit_code": 0 if passed else 1, "detail": detail}


def test_baseline_startup_crash_is_bootstrap() -> None:
    assert classify_verify_failure(_verify(_BASELINE_STARTUP_CRASH)) == "bootstrap"


def test_collection_error_is_bootstrap() -> None:
    assert classify_verify_failure(_verify(_COLLECTION_ERROR)) == "bootstrap"


def test_syntax_error_is_bootstrap() -> None:
    assert classify_verify_failure(_verify(_SYNTAX_ERROR)) == "bootstrap"


def test_assertion_failure_is_test() -> None:
    assert classify_verify_failure(_verify(_ASSERTION_FAILURE)) == "test"


def test_in_test_import_error_stays_fixable() -> None:
    """A ModuleNotFoundError raised after collection succeeded is a real failure."""
    assert classify_verify_failure(_verify(_IN_TEST_IMPORT_ERROR)) == "test"


def test_lint_failure_is_fixable() -> None:
    assert classify_verify_failure(_verify(_LINT_FAILURE)) == "test"


def test_classify_reads_stdout_stderr_when_no_detail() -> None:
    outcome = {"phase": "verify", "passed": False, "stdout": "", "stderr": _BASELINE_STARTUP_CRASH}
    assert classify_verify_failure(outcome) == "bootstrap"


def test_last_failing_verify_drives_the_verdict() -> None:
    """The most recent failing verify decides: a fix that broke the harness wins."""
    results = [
        _verify(_ASSERTION_FAILURE),
        {"phase": "fix", "exit_code": 0},
        _verify(_COLLECTION_ERROR),
    ]
    assert last_verify_failure_is_bootstrap(results) is True

    results_reversed = [_verify(_COLLECTION_ERROR), _verify(_ASSERTION_FAILURE)]
    assert last_verify_failure_is_bootstrap(results_reversed) is False


def test_no_failing_verify_is_not_bootstrap() -> None:
    assert last_verify_failure_is_bootstrap([_verify("ok", passed=True)]) is False
    assert last_verify_failure_is_bootstrap([{"phase": "execute", "exit_code": 0}]) is False


def _run_auto_fix(verify_detail: str) -> MagicMock:
    """Run the auto-fix loop with a patched run_pipeline that fails verify, and
    return the run_fix_phase mock so the caller can assert whether a fix ran."""
    from agent_fleet.code_review.loop import run_code_review_with_auto_fix

    phase_results = [
        {"phase": "execute", "exit_code": 0, "stdout": "done", "stderr": ""},
        {"phase": "scope", "passed": True, "exit_code": 0},
        _verify(verify_detail),
    ]
    config = CodeReviewConfig(
        auto_fix=True, max_fix_attempts=2, fix_persona="coder", review_blocking=False
    )
    fix_mock = MagicMock(return_value={"phase": "fix", "exit_code": 1, "stdout": "", "stderr": ""})

    _loop = "agent_fleet.code_review.loop"
    with (
        patch(
            f"{_loop}.run_pipeline",
            return_value=(list(phase_results), "summary", 1, ["packages/lakestore/tests/t.py"]),
        ),
        patch(f"{_loop}.run_fix_phase", fix_mock),
    ):
        run_code_review_with_auto_fix(
            backend=MagicMock(),
            resolver=MagicMock(),
            task=MagicMock(),
            workspace=MagicMock(),
            timeout_s=60,
            phases=["execute", "review"],
            repo=None,
            config=config,
        )
    return fix_mock


def test_bootstrap_failure_skips_fix_loop() -> None:
    """A harness that cannot start is surfaced, not handed to a fix agent."""
    fix_mock = _run_auto_fix(_COLLECTION_ERROR)
    fix_mock.assert_not_called()


def test_real_test_failure_still_triggers_fix() -> None:
    """A genuine assertion failure is still fixable, so the loop attempts a fix."""
    fix_mock = _run_auto_fix(_ASSERTION_FAILURE)
    fix_mock.assert_called_once()
