"""Tests for the gate contracts — dataclass round-trips and schema validation."""

from __future__ import annotations

import jsonschema
import pytest

from agent_fleet.contracts.gate import (
    Finding,
    FindingsReport,
    GateOutcome,
    GateOutcomeRecord,
    JudgeReport,
    RecheckReport,
    VerifyReport,
    VerifyVerdict,
    validate_findings,
    validate_judge,
    validate_recheck,
    validate_verify,
)

VALID_FINDING = {
    "id": "correctness-1",
    "file": "agent_fleet/gate/pipeline.py",
    "line": 42,
    "claim": "returns the wrong value",
    "repro": "input 1 -> got 0, expected 1",
    "testable": True,
}


def test_finding_round_trip() -> None:
    original = Finding.from_dict(VALID_FINDING)
    assert original.id == "correctness-1"
    assert original.line == 42
    assert original.testable is True
    assert Finding.from_dict(original.to_dict()) == original


def test_finding_from_dict_tolerates_missing_fields() -> None:
    finding = Finding.from_dict({"id": "x"})
    assert finding.file == ""
    assert finding.line == 0
    assert finding.testable is True
    assert finding.lens == ""


def test_findings_report_accepts_an_empty_list() -> None:
    """No blockers is a normal, good outcome — it must validate."""
    report = FindingsReport.from_dict({"findings": []})
    assert report.findings == []
    validate_findings(report.to_dict())


def test_findings_report_ignores_malformed_entries() -> None:
    report = FindingsReport.from_dict({"findings": ["nope", VALID_FINDING, 5]})
    assert len(report.findings) == 1
    assert report.findings[0].id == "correctness-1"


def test_findings_report_ignores_a_non_list() -> None:
    assert FindingsReport.from_dict({"findings": "oops"}).findings == []


def test_validate_findings_rejects_a_missing_repro() -> None:
    """A claim without a repro cannot become a test, so the schema demands one."""
    broken = {"findings": [{k: v for k, v in VALID_FINDING.items() if k != "repro"}]}
    with pytest.raises(jsonschema.ValidationError):
        validate_findings(broken)


def test_validate_findings_rejects_extra_keys() -> None:
    with pytest.raises(jsonschema.ValidationError):
        validate_findings({"findings": [{**VALID_FINDING, "severity": "high"}]})


def test_validate_findings_rejects_a_negative_line() -> None:
    with pytest.raises(jsonschema.ValidationError):
        validate_findings({"findings": [{**VALID_FINDING, "line": -1}]})


def test_verify_verdict_round_trip() -> None:
    report = VerifyReport.from_dict({"verdict": "CONFIRMED", "test_file": "tests/test_gate_a.py"})
    assert report.verdict is VerifyVerdict.CONFIRMED
    assert report.test_file == "tests/test_gate_a.py"
    assert report.to_dict()["verdict"] == "CONFIRMED"


def test_verify_report_omits_a_null_test_file() -> None:
    report = VerifyReport(verdict=VerifyVerdict.REJECTED, test_file=None, reason="no")
    assert "test_file" not in report.to_dict()


def test_verify_report_defaults_to_rejected() -> None:
    assert VerifyReport.from_dict({}).verdict is VerifyVerdict.REJECTED


def test_validate_verify_accepts_the_three_verdicts() -> None:
    for verdict in ("CONFIRMED", "REJECTED", "UNTESTABLE"):
        validate_verify({"verdict": verdict})


def test_validate_verify_rejects_an_unknown_verdict() -> None:
    with pytest.raises(jsonschema.ValidationError):
        validate_verify({"verdict": "MAYBE"})


def test_judge_report_confirmed_untestable() -> None:
    report = JudgeReport.from_dict(
        {
            "untestable_rulings": [
                {"id": "a", "real": True, "reason": "r"},
                {"id": "b", "real": False, "reason": "r"},
            ],
            "new_blockers": [VALID_FINDING],
        }
    )
    assert [r["id"] for r in report.confirmed_untestable] == ["a"]
    assert len(report.new_blockers) == 1


def test_validate_judge_rejects_a_non_boolean_real() -> None:
    with pytest.raises(jsonschema.ValidationError):
        validate_judge(
            {"untestable_rulings": [{"id": "a", "real": "yes", "reason": "r"}], "new_blockers": []}
        )


def test_validate_judge_requires_both_sections() -> None:
    with pytest.raises(jsonschema.ValidationError):
        validate_judge({"untestable_rulings": []})


def test_recheck_report_round_trip() -> None:
    report = RecheckReport.from_dict({"unresolved": [{"id": "a", "reason": "still there"}]})
    assert report.unresolved[0]["id"] == "a"
    assert RecheckReport.from_dict({"unresolved": "bad"}).unresolved == []


def test_validate_recheck_accepts_empty_unresolved() -> None:
    """An empty unresolved list is the all-clear that lets the gate approve."""
    validate_recheck({"unresolved": []})


def test_gate_outcome_record() -> None:
    record = GateOutcomeRecord(outcome=GateOutcome.APPROVED, sha="abc123")
    assert record.to_dict() == {
        "outcome": "APPROVED",
        "sha": "abc123",
        "reasons": [],
    }
