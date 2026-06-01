"""Table-driven tests for decide_disposition — pure, no IO."""

from __future__ import annotations

import pytest

from agent_fleet.disposition import (
    DispositionKind,
    DispositionPolicy,
    RunFacts,
    decide_disposition,
)

_DEFAULT = DispositionPolicy()


@pytest.mark.parametrize(
    ("facts", "policy", "expected_kind", "expected_outcome", "expected_draft"),
    [
        pytest.param(
            RunFacts(
                verify_ok=True, verify_fatal=False, scope_violated=False, changed_files=("a.py",)
            ),
            _DEFAULT,
            DispositionKind.OPEN_PR,
            "completed",
            False,
            id="verify_ok_opens_pr",
        ),
        pytest.param(
            RunFacts(
                verify_ok=False, verify_fatal=True, scope_violated=False, changed_files=("a.py",)
            ),
            _DEFAULT,
            DispositionKind.ABANDON,
            "error",
            False,
            id="verify_fatal_abandons",
        ),
        pytest.param(
            RunFacts(
                verify_ok=False, verify_fatal=False, scope_violated=True, changed_files=("a.py",)
            ),
            _DEFAULT,
            DispositionKind.SALVAGE,
            "scope_violation_salvaged",
            True,
            id="scope_violation_with_files_salvages_when_policy_allows",
        ),
        pytest.param(
            RunFacts(verify_ok=False, verify_fatal=False, scope_violated=True, changed_files=()),
            _DEFAULT,
            DispositionKind.ABANDON,
            "scope_violation",
            False,
            id="scope_violation_no_files_abandons",
        ),
        pytest.param(
            RunFacts(
                verify_ok=False, verify_fatal=False, scope_violated=True, changed_files=("a.py",)
            ),
            DispositionPolicy(salvage_on_scope_violation=False),
            DispositionKind.ABANDON,
            "scope_violation",
            False,
            id="scope_violation_policy_disabled_abandons",
        ),
        pytest.param(
            RunFacts(
                verify_ok=False, verify_fatal=False, scope_violated=False, changed_files=("a.py",)
            ),
            _DEFAULT,
            DispositionKind.SALVAGE,
            "verify_failed_salvaged",
            True,
            id="verify_failed_with_files_salvages_when_policy_allows",
        ),
        pytest.param(
            RunFacts(
                verify_ok=False, verify_fatal=False, scope_violated=False, changed_files=("a.py",)
            ),
            DispositionPolicy(salvage_on_verify_failed=False),
            DispositionKind.ABANDON,
            "verify_failed",
            False,
            id="verify_failed_policy_disabled_abandons",
        ),
        pytest.param(
            RunFacts(verify_ok=False, verify_fatal=False, scope_violated=False, changed_files=()),
            _DEFAULT,
            DispositionKind.NOOP,
            "completed_noop",
            False,
            id="no_changes_noop",
        ),
        pytest.param(
            RunFacts(verify_ok=False, verify_fatal=True, scope_violated=False, changed_files=()),
            _DEFAULT,
            DispositionKind.ABANDON,
            "error",
            False,
            id="verify_fatal_no_files_still_abandons",
        ),
    ],
)
def test_decide_disposition(
    facts: RunFacts,
    policy: DispositionPolicy,
    expected_kind: DispositionKind,
    expected_outcome: str,
    expected_draft: bool,
) -> None:
    d = decide_disposition(facts, policy)
    assert d.kind == expected_kind
    assert d.outcome == expected_outcome
    assert d.draft == expected_draft
    assert d.reason


def test_salvage_labels_default() -> None:
    policy = DispositionPolicy()
    assert "fleet-salvage" in policy.salvage_labels


def test_salvage_labels_customizable() -> None:
    policy = DispositionPolicy(salvage_labels=["custom-label"])
    facts = RunFacts(
        verify_ok=False,
        verify_fatal=False,
        scope_violated=False,
        changed_files=("a.py",),
    )
    d = decide_disposition(facts, policy)
    assert d.kind == DispositionKind.SALVAGE
