"""Unit tests for silphco.selfimprove.mine — deterministic mining logic."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from silphco.selfimprove.mine import (
    ErrorClass,
    FailureSignature,
    SignatureBucket,
    bucket_failures,
    classify_error,
    load_failure_records,
    mine,
    rank_signatures,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_record(
    *,
    persona: str = "backend",
    phase: str = "verify",
    status: str = "failed",
    event: str = "phase_end",
    detail: str | None = None,
    duration_s: float | None = 60.0,
    ts: str | None = None,
    run_id: str = "r001",
    issue: int = 1,
) -> dict:
    if ts is None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    record: dict = {
        "ts": ts,
        "run_id": run_id,
        "issue": issue,
        "persona": persona,
        "event": event,
        "phase": phase,
        "status": status,
        "duration_s": duration_s,
    }
    if detail is not None:
        record["detail"] = detail
    return record


# ---------------------------------------------------------------------------
# classify_error
# ---------------------------------------------------------------------------

class TestClassifyError:
    def test_none_returns_other(self):
        assert classify_error(None) is ErrorClass.OTHER

    def test_empty_returns_other(self):
        assert classify_error("") is ErrorClass.OTHER

    def test_schema_validation_failed(self):
        assert classify_error("schema_validation_failed: missing field") is ErrorClass.SCHEMA_VALIDATION_FAILED

    def test_schema_validation_case_insensitive(self):
        assert classify_error("Schema Validation Failed") is ErrorClass.SCHEMA_VALIDATION_FAILED

    def test_json_schema_matches_schema_validation(self):
        assert classify_error("jsonschema validation error") is ErrorClass.SCHEMA_VALIDATION_FAILED

    def test_verify_rejected(self):
        assert classify_error("verify_rejected: severity=high") is ErrorClass.VERIFY_REJECTED

    def test_review_changes_requested(self):
        assert classify_error("review changes_requested by tech_lead") is ErrorClass.REVIEW_CHANGES_REQUESTED

    def test_timeout(self):
        assert classify_error("subprocess timed out after 120s") is ErrorClass.TIMEOUT

    def test_timeout_deadline(self):
        assert classify_error("deadline exceeded") is ErrorClass.TIMEOUT

    def test_tool_error(self):
        assert classify_error("tool_error: read_file failed") is ErrorClass.TOOL_ERROR

    def test_git_commit_failed(self):
        assert classify_error("git commit failed: nothing to commit") is ErrorClass.GIT_COMMIT_FAILED

    def test_zero_diff(self):
        assert classify_error("zero_diff: no changes produced") is ErrorClass.ZERO_DIFF

    def test_empty_diff(self):
        assert classify_error("empty diff returned") is ErrorClass.ZERO_DIFF

    def test_other(self):
        assert classify_error("something completely unexpected") is ErrorClass.OTHER

    def test_first_pattern_wins(self):
        # "schema_validation_failed" must match SCHEMA before OTHER
        assert classify_error("schema_validation_failed timeout") is ErrorClass.SCHEMA_VALIDATION_FAILED


# ---------------------------------------------------------------------------
# bucket_failures
# ---------------------------------------------------------------------------

class TestBucketFailures:
    def test_empty_input_returns_empty_dict(self):
        assert bucket_failures([]) == {}

    def test_single_record_produces_one_bucket(self):
        records = [_make_record()]
        buckets = bucket_failures(records)
        assert len(buckets) == 1
        sig = FailureSignature(persona="backend", phase="verify", error_class=ErrorClass.OTHER)
        assert sig in buckets
        assert buckets[sig].count == 1

    def test_groups_same_signature(self):
        records = [
            _make_record(persona="backend", phase="verify", detail=None, run_id="r1"),
            _make_record(persona="backend", phase="verify", detail=None, run_id="r2"),
            _make_record(persona="backend", phase="verify", detail=None, run_id="r3"),
        ]
        buckets = bucket_failures(records)
        sig = FailureSignature(persona="backend", phase="verify", error_class=ErrorClass.OTHER)
        assert buckets[sig].count == 3

    def test_separates_different_personas(self):
        records = [
            _make_record(persona="backend", phase="verify"),
            _make_record(persona="frontend", phase="verify"),
        ]
        buckets = bucket_failures(records)
        assert len(buckets) == 2

    def test_separates_different_phases(self):
        records = [
            _make_record(persona="backend", phase="verify"),
            _make_record(persona="backend", phase="implement"),
        ]
        buckets = bucket_failures(records)
        assert len(buckets) == 2

    def test_error_class_derived_from_detail(self):
        records = [_make_record(detail="schema_validation_failed: missing key")]
        buckets = bucket_failures(records)
        sig = FailureSignature(
            persona="backend", phase="verify",
            error_class=ErrorClass.SCHEMA_VALIDATION_FAILED,
        )
        assert sig in buckets

    def test_cost_accumulates_duration(self):
        records = [
            _make_record(duration_s=30.0, run_id="r1"),
            _make_record(duration_s=60.0, run_id="r2"),
        ]
        buckets = bucket_failures(records)
        sig = FailureSignature(persona="backend", phase="verify", error_class=ErrorClass.OTHER)
        assert buckets[sig].total_cost == pytest.approx(90.0)

    def test_missing_duration_defaults_to_1(self):
        records = [_make_record(duration_s=None)]
        buckets = bucket_failures(records)
        sig = FailureSignature(persona="backend", phase="verify", error_class=ErrorClass.OTHER)
        assert buckets[sig].total_cost == pytest.approx(1.0)

    def test_record_without_persona_is_skipped(self):
        record = _make_record()
        del record["persona"]
        assert bucket_failures([record]) == {}

    def test_record_without_phase_is_skipped(self):
        record = _make_record()
        del record["phase"]
        assert bucket_failures([record]) == {}

    def test_traces_stored_in_bucket(self):
        records = [_make_record(run_id="r1"), _make_record(run_id="r2")]
        buckets = bucket_failures(records)
        sig = FailureSignature(persona="backend", phase="verify", error_class=ErrorClass.OTHER)
        trace_run_ids = [t.run_id for t in buckets[sig].traces]
        assert "r1" in trace_run_ids
        assert "r2" in trace_run_ids


# ---------------------------------------------------------------------------
# rank_signatures
# ---------------------------------------------------------------------------

class TestRankSignatures:
    def _make_bucket(
        self,
        persona: str,
        phase: str,
        count: int,
        cost: float,
    ) -> tuple[FailureSignature, SignatureBucket]:
        sig = FailureSignature(persona=persona, phase=phase, error_class=ErrorClass.OTHER)
        b = SignatureBucket(signature=sig, count=count, total_cost=cost)
        return sig, b

    def test_empty_buckets_returns_empty_list(self):
        assert rank_signatures({}) == []

    def test_below_threshold_excluded(self):
        sig, bucket = self._make_bucket("backend", "verify", count=3, cost=10.0)
        result = rank_signatures({sig: bucket}, min_occurrences=5)
        assert result == []

    def test_at_threshold_included(self):
        sig, bucket = self._make_bucket("backend", "verify", count=5, cost=10.0)
        result = rank_signatures({sig: bucket}, min_occurrences=5)
        assert len(result) == 1

    def test_sorted_by_score_descending(self):
        sig1, b1 = self._make_bucket("backend", "verify", count=5, cost=10.0)   # score=50
        sig2, b2 = self._make_bucket("frontend", "implement", count=10, cost=20.0)  # score=200
        sig3, b3 = self._make_bucket("data", "research", count=6, cost=5.0)     # score=30
        result = rank_signatures({sig1: b1, sig2: b2, sig3: b3}, min_occurrences=5)
        scores = [r.score for r in result]
        assert scores == sorted(scores, reverse=True)
        assert result[0].signature == sig2

    def test_default_min_occurrences_is_5(self):
        sig4, b4 = self._make_bucket("backend", "plan", count=4, cost=100.0)
        sig5, b5 = self._make_bucket("backend", "plan", count=5, cost=1.0)
        # Only sig5 passes (count=5 >= 5)
        result = rank_signatures({sig4: b4, sig5: b5})
        assert len(result) == 1
        assert result[0].signature == sig5


# ---------------------------------------------------------------------------
# load_failure_records — file I/O
# ---------------------------------------------------------------------------

class TestLoadFailureRecords:
    def test_missing_file_returns_empty(self, tmp_path: Path):
        result = load_failure_records(tmp_path / "nonexistent.jsonl")
        assert result == []

    def test_empty_file_returns_empty(self, tmp_path: Path):
        f = tmp_path / "log.jsonl"
        f.write_text("")
        result = load_failure_records(f)
        assert result == []

    def test_malformed_lines_skipped(self, tmp_path: Path):
        f = tmp_path / "log.jsonl"
        f.write_text("not json\n{\"a\":1}\n")
        result = load_failure_records(f)
        # The valid line is not a failure record, malformed is skipped
        assert result == []

    def test_loads_failure_records_within_window(self, tmp_path: Path):
        now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)
        recent_ts = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        old_ts = (now - timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%SZ")

        f = tmp_path / "log.jsonl"
        records = [
            _make_record(ts=recent_ts, event="phase_end", status="failed"),
            _make_record(ts=old_ts, event="phase_end", status="failed"),
        ]
        f.write_text("\n".join(json.dumps(r) for r in records) + "\n")

        result = load_failure_records(f, days=30, now=now)
        assert len(result) == 1

    def test_non_failure_records_excluded(self, tmp_path: Path):
        now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)
        ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        f = tmp_path / "log.jsonl"
        records = [
            _make_record(ts=ts, event="phase_end", status="complete"),
            _make_record(ts=ts, event="run_start", status="started"),
            _make_record(ts=ts, event="phase_end", status="failed"),
        ]
        f.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        result = load_failure_records(f, days=30, now=now)
        assert len(result) == 1

    def test_directory_layout_reads_ndjson_files(self, tmp_path: Path):
        now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)
        today_str = now.strftime("%Y-%m-%d")
        ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        ndjson_file = tmp_path / f"{today_str}.ndjson"
        record = _make_record(ts=ts, event="phase_end", status="failed")
        ndjson_file.write_text(json.dumps(record) + "\n")

        result = load_failure_records(tmp_path, days=7, now=now)
        assert len(result) == 1

    def test_run_end_failure_included(self, tmp_path: Path):
        now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)
        ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        f = tmp_path / "log.jsonl"
        record = _make_record(ts=ts, event="run_end", status="failed")
        f.write_text(json.dumps(record) + "\n")
        result = load_failure_records(f, days=30, now=now)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# mine — integration
# ---------------------------------------------------------------------------

class TestMineIntegration:
    def test_mine_below_threshold_returns_empty(self, tmp_path: Path):
        now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)
        ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        f = tmp_path / "log.jsonl"
        # Only 3 records; threshold is 5
        records = [_make_record(ts=ts, run_id=f"r{i}") for i in range(3)]
        f.write_text("\n".join(json.dumps(r) for r in records) + "\n")

        result = mine(f, days=30, min_occurrences=5, now=now)
        assert result == []

    def test_mine_above_threshold_returns_sorted_buckets(self, tmp_path: Path):
        now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)
        ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        f = tmp_path / "log.jsonl"
        # 6 backend/verify records, 5 frontend/implement records
        records = (
            [_make_record(ts=ts, persona="backend", phase="verify", duration_s=100.0, run_id=f"r{i}") for i in range(6)]
            + [_make_record(ts=ts, persona="frontend", phase="implement", duration_s=10.0, run_id=f"r{i+100}") for i in range(5)]
        )
        f.write_text("\n".join(json.dumps(r) for r in records) + "\n")

        result = mine(f, days=30, min_occurrences=5, now=now)
        assert len(result) == 2
        # backend/verify score = 6 * 600 = 3600; frontend/implement = 5 * 50 = 250
        assert result[0].signature.persona == "backend"
        assert result[0].signature.phase == "verify"

    def test_mine_tolerates_missing_file(self, tmp_path: Path):
        result = mine(tmp_path / "does_not_exist.jsonl", days=30, min_occurrences=5)
        assert result == []
