"""Tests for the gate pipeline: step0, verify's trust-the-test rule, convergence,
and the outcome/status-line contract.

The gate's expensive parts (backends, pytest, git) are faked at their seams, so
these tests exercise the pipeline's own decision logic — which is where the
value is: a claim only becomes a blocker with a failing test, and a fix round
continues only while the failing set measurably shrinks.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 - built into concrete paths at runtime
from typing import Any

import pytest

from agent_fleet.contracts.gate import Finding
from agent_fleet.gate import metrics as gm
from agent_fleet.gate.config import GateConfig
from agent_fleet.gate.pipeline import (
    GateInfraError,
    GatePipeline,
    GateTestArchive,
    GateTestRunner,
    TestRun,
    _file_of_node_id,
    _json_blob,
    _normalise_repo_path,
    status_line_for,
)
from agent_fleet.model_policy import ModelPolicy
from agent_fleet.slots import SlotPool

_PYPROJECT = """[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[project]
name = "gate-fixture"
version = "0.0.0"
"""

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeBackend:
    """Replays a scripted answer per prompt match; records every call."""

    answers: dict[str, str] = field(default_factory=dict)
    default: str = ""
    prompts: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)

    def run(self, prompt: str, **_kwargs: Any) -> Any:  # noqa: ANN401
        self.prompts.append(prompt)
        self.models.append(_kwargs.get("model", ""))
        for needle, answer in self.answers.items():
            if needle in prompt:
                return _FakeResult(answer)
        return _FakeResult(self.default)

    def prompts_containing(self, needle: str) -> list[str]:
        return [p for p in self.prompts if needle in p]


@dataclass(frozen=True)
class _FakeResult:
    stdout: str
    stderr: str = ""
    exit_code: int = 0
    duration_s: float = 0.0
    agent_id: str | None = None
    usage: dict[str, int] | None = None


def _findings_json(*ids: str) -> str:
    payload = {
        "findings": [
            {
                "id": fid,
                "file": "agent_fleet/gate/pipeline.py",
                "line": 10,
                "claim": f"defect {fid}",
                "repro": "input -> wrong",
                "testable": True,
            }
            for fid in ids
        ]
    }
    return json.dumps(payload)


def _finding(fid: str = "c-1", *, testable: bool = True) -> Finding:
    return Finding(
        id=fid,
        file="agent_fleet/gate/pipeline.py",
        line=10,
        claim=f"defect {fid}",
        repro="input -> wrong",
        testable=testable,
    )


def _policy() -> ModelPolicy:
    return ModelPolicy(backends={})


def _config(**overrides: Any) -> GateConfig:  # noqa: ANN401
    base: dict[str, Any] = {
        "backend": "cmd",
        "model": "m",
        "judge_backend": "cmd",
        "judge_model": "m",
        "enable_judge": False,
        "enable_fix": True,
        "agent_timeout_s": 10,
        "test_timeout_s": 10,
    }
    base.update(overrides)
    return GateConfig(**base)


def _pipeline(
    tmp_path: Path,
    backend: _FakeBackend,
    *,
    config: GateConfig | None = None,
    judge_backend: _FakeBackend | None = None,
) -> GatePipeline:
    return GatePipeline(
        repo=tmp_path / "repo",
        pr_number=42,
        config=config or _config(),
        policy=_policy(),
        backend=backend,  # type: ignore[arg-type]
        judge_backend=judge_backend,  # type: ignore[arg-type]
        gate_dir=tmp_path / "gate",
        use_systemd=False,
    )


# ---------------------------------------------------------------------------
# Status line — the automerge contract
# ---------------------------------------------------------------------------


def test_approved_status_line_carries_the_nine_char_sha() -> None:
    line = status_line_for(_approved(), "abcdef1234567890", [])
    assert re.fullmatch(r"\d{2}:\d{2}:\d{2} PREMERGE-APPROVED abcdef123", line)


def test_escalation_status_line_carries_the_first_reason() -> None:
    from agent_fleet.contracts.gate import GateOutcome

    line = status_line_for(GateOutcome.NEEDS_ESCALATION, "abc", ["stalled after 2 round(s)", "x"])
    assert "NEEDS-ESCALATION stalled after 2 round(s)" in line
    assert "abc" not in line  # an escalation does not bless a sha


def test_escalation_without_a_reason_says_unspecified() -> None:
    from agent_fleet.contracts.gate import GateOutcome

    assert "unspecified" in status_line_for(GateOutcome.NEEDS_ESCALATION, "", [])


def _approved():  # noqa: ANN202
    from agent_fleet.contracts.gate import GateOutcome

    return GateOutcome.APPROVED


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_file_of_node_id_maps_back_to_the_test_file() -> None:
    files = ["tests/test_a.py", "api/tests/test_b.py"]
    assert _file_of_node_id("tests/test_a.py::test_x", files) == "tests/test_a.py"
    assert _file_of_node_id("api/tests/test_b.py::test_y", files) == "api/tests/test_b.py"
    assert _file_of_node_id("tests/unknown.py::t", files) is None


def test_normalise_repo_path_strips_absolute_and_dot_prefixes() -> None:
    assert _normalise_repo_path("./tests/test_a.py") == "tests/test_a.py"
    assert _normalise_repo_path("  tests/test_a.py  ") == "tests/test_a.py"
    assert _normalise_repo_path("tests\\test_a.py") == "tests/test_a.py"


def test_json_blob_renders_none_for_empty() -> None:
    assert _json_blob([], 100) == "(none)"


def test_json_blob_truncates() -> None:
    assert len(_json_blob([{"a": "x" * 500}], 50)) == 50


# ---------------------------------------------------------------------------
# GateTestRunner
# ---------------------------------------------------------------------------


def test_runner_with_no_tests_passes(tmp_path: Path) -> None:
    runner = GateTestRunner(root=tmp_path, use_systemd=False)
    run = runner.run([])
    assert run.count == 0
    assert not run.infra_error
    assert not run.tests_failed


def test_runner_reports_a_passing_suite(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (tmp_path / "test_ok.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    run = GateTestRunner(root=tmp_path, use_systemd=False, timeout_s=300).run(["test_ok.py"])
    assert run.count == 0
    assert not run.tests_failed
    assert run.ran == 1


def test_runner_reports_failing_node_ids(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (tmp_path / "test_bad.py").write_text(
        "def test_one():\n    assert False\n\n\ndef test_two():\n    assert False\n",
        encoding="utf-8",
    )
    run = GateTestRunner(root=tmp_path, use_systemd=False, timeout_s=300).run(["test_bad.py"])
    assert run.count == 2
    assert run.tests_failed
    assert not run.infra_error


def test_runner_treats_a_collection_error_as_infra_not_a_finding(tmp_path: Path) -> None:
    """The load-bearing distinction: we learned nothing, so it is not a blocker."""
    (tmp_path / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (tmp_path / "test_broken.py").write_text("def test_x(:\n", encoding="utf-8")
    run = GateTestRunner(root=tmp_path, use_systemd=False, timeout_s=300).run(["test_broken.py"])
    assert run.infra_error
    assert run.count == 0


def test_pytest_hint_names_the_owning_package(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    runner = GateTestRunner(root=tmp_path, use_systemd=False)
    hint = runner.pytest_hint("api/tests/test_a.py")
    assert str(tmp_path / "api") in hint
    assert "test_a.py" in hint
    assert runner.test_dir_hint("api/tests/test_a.py") == "api/tests"


def test_runner_uses_a_slot_for_each_pytest(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (tmp_path / "test_ok.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    pool = SlotPool("test", root=tmp_path / "slots", size=1)
    runner = GateTestRunner(root=tmp_path, pool=pool, use_systemd=False, timeout_s=300)
    run = runner.run(["test_ok.py"])
    assert not run.infra_error
    assert pool.in_use() == 0  # the slot is released after the run


# ---------------------------------------------------------------------------
# GateTestArchive
# ---------------------------------------------------------------------------


def test_archive_round_trips_a_gate_test(tmp_path: Path) -> None:
    archive = GateTestArchive(tmp_path)
    source = tmp_path / "src" / "test_gate_a.py"
    source.parent.mkdir(parents=True)
    source.write_text("def test_x():\n    assert False\n", encoding="utf-8")
    assert archive.store(source) is not None

    fresh = tmp_path / "next"
    (fresh / "tests").mkdir(parents=True)
    restored = archive.materialise(fresh, ["tests/test_gate_a.py"])
    assert restored == ["tests/test_gate_a.py"]
    assert (fresh / "tests" / "test_gate_a.py").is_file()


def test_archive_does_not_overwrite_an_existing_file(tmp_path: Path) -> None:
    """If the fixer committed the test itself, keep their version."""
    archive = GateTestArchive(tmp_path)
    source = tmp_path / "test_gate_a.py"
    source.write_text("archived", encoding="utf-8")
    archive.store(source)

    fresh = tmp_path / "next"
    (fresh / "tests").mkdir(parents=True)
    (fresh / "tests" / "test_gate_a.py").write_text("committed", encoding="utf-8")
    assert archive.materialise(fresh, ["tests/test_gate_a.py"]) == []
    assert (fresh / "tests" / "test_gate_a.py").read_text() == "committed"


def test_archive_store_ignores_a_missing_source(tmp_path: Path) -> None:
    assert GateTestArchive(tmp_path).store(tmp_path / "nope.py") is None


# ---------------------------------------------------------------------------
# step0 — the PR's own tests at head
# ---------------------------------------------------------------------------


class _StubPipeline(GatePipeline):
    """A GatePipeline with run_pr_tests' git dependency injected."""

    def __init__(self, tmp_path: Path, config: GateConfig | None = None) -> None:
        super().__init__(
            repo=tmp_path / "repo",
            pr_number=1,
            config=config or _config(),
            policy=_policy(),
            backend=_FakeBackend(),  # type: ignore[arg-type]
            gate_dir=tmp_path / "gate",
            use_systemd=False,
        )
        self.gate_dir.mkdir(parents=True, exist_ok=True)


def test_step0_records_each_failing_test_as_a_confirmed_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PR test failing at head is a blocker with no interpretation step."""
    pipe = _StubPipeline(tmp_path)
    monkeypatch.setattr(
        "agent_fleet.gate.pipeline.changed_test_files",
        lambda *_a: ["tests/test_a.py"],
    )

    def _fake_run(_self: GateTestRunner, _files: list[str]) -> TestRun:
        return TestRun(failing=["tests/test_a.py::test_x", "tests/test_a.py::test_y"], ran=1)

    monkeypatch.setattr(GateTestRunner, "run", _fake_run)
    assert pipe.run_pr_tests(tmp_path / "wt") == ["tests/test_a.py"]
    assert len(pipe.evidence.confirmed) == 2
    assert all(c["source"] == "pr-tests" for c in pipe.evidence.confirmed)
    assert pipe.evidence.confirmed[0]["test_file"] == "tests/test_a.py"


def test_step0_raises_on_an_infra_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A suite that could not run must escalate, never pass by default."""
    pipe = _StubPipeline(tmp_path)
    monkeypatch.setattr(
        "agent_fleet.gate.pipeline.changed_test_files",
        lambda *_a: ["tests/test_a.py"],
    )
    monkeypatch.setattr(
        GateTestRunner,
        "run",
        lambda _self, _files: TestRun(infra_error="collection error", ran=1),
    )
    with pytest.raises(GateInfraError, match="collection error"):
        pipe.run_pr_tests(tmp_path / "wt")


def test_step0_with_no_changed_tests_is_a_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipe = _StubPipeline(tmp_path)
    monkeypatch.setattr("agent_fleet.gate.pipeline.changed_test_files", lambda *_a: [])
    assert pipe.run_pr_tests(tmp_path / "wt") == []
    assert pipe.evidence.confirmed == []


# ---------------------------------------------------------------------------
# verify — the pipeline trusts the test, not the verdict
# ---------------------------------------------------------------------------


def _verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    verdict_payload: str,
    written_test: str | None,
    test_result: TestRun,
) -> tuple[GatePipeline, Path]:
    """Drive one verify with a scripted answer and a scripted test outcome.

    The verifier agent is the one that *writes* the test file, so *written_test*
    is materialised in the worktree before verify inspects it — the pipeline
    checks the file exists precisely because the agent was asked to create it.
    """
    backend = _FakeBackend(answers={"You verify ONE claimed defect": verdict_payload})
    pipe = _pipeline(tmp_path, backend)
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True, exist_ok=True)
    if written_test:
        target = worktree / written_test
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def test_x():\n    assert False\n", encoding="utf-8")

    monkeypatch.setattr(GateTestRunner, "run", lambda _self, _files: test_result)
    return pipe, worktree


def test_verify_confirms_when_the_test_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CONFIRMED + a test that exits 1 is the only path to a blocker."""
    pipe, wt = _verify(
        tmp_path,
        monkeypatch,
        verdict_payload=json.dumps(
            {"verdict": "CONFIRMED", "test_file": "tests/test_gate_c_1.py", "reason": "boom"}
        ),
        written_test="tests/test_gate_c_1.py",
        test_result=TestRun(failing=["tests/test_gate_c_1.py::test_x"], ran=1, tests_failed=True),
    )
    pipe.verify(wt, [_finding()], source="lens")
    assert len(pipe.evidence.confirmed) == 1
    assert pipe.evidence.confirmed[0]["test_file"] == "tests/test_gate_c_1.py"
    assert pipe.evidence.gate_tests == ["tests/test_gate_c_1.py"]


def test_verify_discards_a_confirmed_verdict_whose_test_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verifier said CONFIRMED but its test exits 0 — the claim is refuted."""
    pipe, wt = _verify(
        tmp_path,
        monkeypatch,
        verdict_payload=json.dumps({"verdict": "CONFIRMED", "test_file": "tests/test_gate_c_1.py"}),
        written_test="tests/test_gate_c_1.py",
        test_result=TestRun(ran=1, tests_failed=False),
    )
    pipe.verify(wt, [_finding()], source="lens")
    assert pipe.evidence.confirmed == []
    assert pipe.evidence.rejected == 1
    # The refuting test must not linger in the worktree.
    assert not (wt / "tests" / "test_gate_c_1.py").exists()


def test_verify_records_untestable_for_the_judge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipe, wt = _verify(
        tmp_path,
        monkeypatch,
        verdict_payload=json.dumps({"verdict": "UNTESTABLE", "reason": "needs prod volume"}),
        written_test=None,
        test_result=TestRun(ran=0),
    )
    pipe.verify(wt, [_finding()], source="lens")
    assert len(pipe.evidence.untestable) == 1
    assert pipe.evidence.confirmed == []
    assert pipe.evidence.rejected == 0


def test_verify_counts_a_rejected_verdict_as_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipe, wt = _verify(
        tmp_path,
        monkeypatch,
        verdict_payload=json.dumps({"verdict": "REJECTED", "reason": "code is correct"}),
        written_test=None,
        test_result=TestRun(ran=0),
    )
    pipe.verify(wt, [_finding()], source="lens")
    assert pipe.evidence.rejected == 1
    assert pipe.evidence.confirmed == []


def test_verify_discards_a_confirmed_verdict_with_no_test_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipe, wt = _verify(
        tmp_path,
        monkeypatch,
        verdict_payload=json.dumps({"verdict": "CONFIRMED", "test_file": None}),
        written_test=None,
        test_result=TestRun(ran=0),
    )
    pipe.verify(wt, [_finding()], source="lens")
    assert pipe.evidence.rejected == 1


def test_verify_discards_when_the_named_file_does_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipe, wt = _verify(
        tmp_path,
        monkeypatch,
        verdict_payload=json.dumps({"verdict": "CONFIRMED", "test_file": "tests/never_written.py"}),
        written_test=None,
        test_result=TestRun(ran=0),
    )
    pipe.verify(wt, [_finding()], source="lens")
    assert pipe.evidence.rejected == 1


def test_verify_discards_when_the_test_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An import error in the verifier's test is an infra failure, not a blocker."""
    pipe, wt = _verify(
        tmp_path,
        monkeypatch,
        verdict_payload=json.dumps({"verdict": "CONFIRMED", "test_file": "tests/test_gate_c_1.py"}),
        written_test="tests/test_gate_c_1.py",
        test_result=TestRun(infra_error="import error", ran=1),
    )
    pipe.verify(wt, [_finding()], source="lens")
    assert pipe.evidence.rejected == 1
    assert pipe.evidence.confirmed == []


def test_verify_skips_untestable_claims_without_calling_a_verifier(
    tmp_path: Path,
) -> None:
    """A reviewer that said testable: false is routed to the judge, not verified."""
    backend = _FakeBackend()
    pipe = _pipeline(tmp_path, backend)
    pipe.verify(tmp_path / "wt", [_finding(testable=False)], source="lens")
    assert backend.prompts == []
    assert pipe.evidence.confirmed == []


def test_verify_counts_an_unparseable_answer_as_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An answer without proof leaves the claim unproven: rejected, not escalated."""
    pipe, wt = _verify(
        tmp_path,
        monkeypatch,
        verdict_payload="I could not determine that.",
        written_test=None,
        test_result=TestRun(ran=0),
    )
    pipe.verify(wt, [_finding()], source="lens")
    assert pipe.evidence.rejected == 1


def test_verify_fails_closed_when_the_verifier_dies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead verifier (empty output) must not silently drop a possible blocker."""
    pipe, wt = _verify(
        tmp_path,
        monkeypatch,
        verdict_payload="",
        written_test=None,
        test_result=TestRun(ran=0),
    )
    with pytest.raises(GateInfraError, match="fail-closed: verify"):
        pipe.verify(wt, [_finding()], source="lens")


# ---------------------------------------------------------------------------
# judge — at most one call, and its own claims go back through verify
# ---------------------------------------------------------------------------


def test_judge_rules_on_untestable_and_verifies_its_own_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    judge_payload = json.dumps(
        {
            "untestable_rulings": [
                {"id": "c-1", "real": True, "reason": "real prod risk"},
                {"id": "c-2", "real": False, "reason": "not a blocker"},
            ],
            "new_blockers": [
                {
                    "id": "j-1",
                    "file": "agent_fleet/x.py",
                    "line": 1,
                    "claim": "new defect",
                    "repro": "input -> wrong",
                    "testable": True,
                }
            ],
        }
    )
    backend = _FakeBackend(answers={"You verify ONE claimed defect": _findings_json("j-1")})
    backend.answers["You are the final pre-merge judge"] = judge_payload
    pipe = _pipeline(
        tmp_path,
        backend,
        config=_config(enable_judge=True),
        judge_backend=backend,
    )
    pipe.evidence.untestable.append(_finding("c-1").to_dict())
    pipe.evidence.untestable.append(_finding("c-2").to_dict())

    # The verifier answer for the judge's new claim must be a real failing test.
    backend.answers["You verify ONE claimed defect"] = json.dumps(
        {"verdict": "CONFIRMED", "test_file": "tests/test_gate_j_1.py"}
    )

    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True, exist_ok=True)
    # The judge's new blocker goes through verify, so its "verifier" wrote a test.
    judge_test = worktree / "tests" / "test_gate_j_1.py"
    judge_test.parent.mkdir(parents=True, exist_ok=True)
    judge_test.write_text("def test_x():\n    assert False\n", encoding="utf-8")

    monkeypatch.setattr(
        GateTestRunner,
        "run",
        lambda _self, _files: TestRun(
            failing=["tests/test_gate_j_1.py::test_x"], ran=1, tests_failed=True
        ),
    )
    pipe.judge(worktree, _ref())

    judge_calls = backend.prompts_containing("You are the final pre-merge judge")
    assert len(judge_calls) == 1
    # Only the "real" ruling is promoted to a blocker.
    confirmed_untestable = [
        c for c in pipe.evidence.confirmed if c.get("source") == "judge-untestable"
    ]
    assert len(confirmed_untestable) == 1
    assert confirmed_untestable[0]["id"] == "c-1"
    # The judge's own blocker had to earn its place with a failing test.
    assert any(c.get("test_file") == "tests/test_gate_j_1.py" for c in pipe.evidence.confirmed)


def test_judge_is_skipped_when_disabled(tmp_path: Path) -> None:
    backend = _FakeBackend()
    pipe = _pipeline(tmp_path, backend, config=_config(enable_judge=False), judge_backend=backend)
    pipe.judge(tmp_path / "wt", _ref())
    assert backend.prompts == []


def test_judge_failure_fails_closed(tmp_path: Path) -> None:
    """A judge we could not reach is not a ruling: escalate, never approve."""
    backend = _FakeBackend(answers={"final pre-merge judge": "no idea"})
    pipe = _pipeline(tmp_path, backend, config=_config(enable_judge=True), judge_backend=backend)
    pipe.evidence.untestable.append(_finding("c-1").to_dict())
    with pytest.raises(GateInfraError, match="fail-closed: judge"):
        pipe.judge(tmp_path / "wt", _ref())
    assert pipe.evidence.confirmed == []


# ---------------------------------------------------------------------------
# recheck
# ---------------------------------------------------------------------------


def test_recheck_returns_true_when_nothing_is_unresolved(
    tmp_path: Path,
) -> None:
    backend = _FakeBackend(answers={"Recheck for PR": json.dumps({"unresolved": []})})
    pipe = _pipeline(tmp_path, backend, config=_config(enable_judge=True), judge_backend=backend)
    pipe.evidence.confirmed.append(
        {"id": "c-1", "source": "judge-untestable", "claim": "real", "test_file": None}
    )
    assert pipe.recheck_untestable(tmp_path / "wt", "a" * 40, "b" * 40) is True


def test_recheck_returns_false_when_a_blocker_remains(
    tmp_path: Path,
) -> None:
    backend = _FakeBackend(
        answers={
            "Recheck for PR": json.dumps({"unresolved": [{"id": "c-1", "reason": "still broken"}]})
        }
    )
    pipe = _pipeline(tmp_path, backend, config=_config(enable_judge=True), judge_backend=backend)
    pipe.evidence.confirmed.append(
        {"id": "c-1", "source": "judge-untestable", "claim": "real", "test_file": None}
    )
    assert pipe.recheck_untestable(tmp_path / "wt", "a" * 40, "b" * 40) is False


def test_recheck_is_a_noop_without_untestable_claims(tmp_path: Path) -> None:
    backend = _FakeBackend()
    pipe = _pipeline(tmp_path, backend, config=_config(enable_judge=True), judge_backend=backend)
    assert pipe.recheck_untestable(tmp_path / "wt", "a" * 40, "b" * 40) is True
    assert backend.prompts == []


def test_recheck_failure_is_not_an_all_clear(tmp_path: Path) -> None:
    backend = _FakeBackend(answers={"Recheck for PR": "garbage"})
    pipe = _pipeline(tmp_path, backend, config=_config(enable_judge=True), judge_backend=backend)
    pipe.evidence.confirmed.append(
        {"id": "c-1", "source": "judge-untestable", "claim": "real", "test_file": None}
    )
    assert pipe.recheck_untestable(tmp_path / "wt", "a" * 40, "b" * 40) is False


def _ref():  # noqa: ANN202
    from agent_fleet.gate.gitops import PullRequestRef

    return PullRequestRef(number=42, head_ref="fb/lane", head_sha="a" * 40, state="OPEN")


# ---------------------------------------------------------------------------
# find — dedupe across lenses
# ---------------------------------------------------------------------------


def test_find_dedupes_the_same_claim_from_two_lenses(tmp_path: Path) -> None:
    """Four lenses often rediscover the same defect; it must be verified once."""
    same = {
        "findings": [
            {
                "id": "x-1",
                "file": "a/agent_fleet/gate/pipeline.py",
                "line": 1,
                "claim": "Division by zero on empty input",
                "repro": "r",
                "testable": True,
            }
        ]
    }
    backend = _FakeBackend(default=json.dumps(same))
    pipe = _pipeline(tmp_path, backend)
    found = pipe.find(tmp_path / "wt", _ref())
    assert len(found) == 1
    # One lens ran per configured lens, all in parallel.
    assert len(backend.prompts) == len(pipe.config.lenses)


def test_find_fails_closed_when_a_lens_dies(tmp_path: Path) -> None:
    """A killed reviewer is not a clean review: 0 findings from a dead lens must escalate.

    Regression: on 2026-09-25 a mass SIGKILL of agent wrappers left every lens
    empty and the gate approved a PR on "0 candidates".
    """
    backend = _FakeBackend(answers={"**correctness**": _findings_json("c-1")}, default="")
    pipe = _pipeline(tmp_path, backend)
    with pytest.raises(GateInfraError, match="fail-closed: lens"):
        pipe.find(tmp_path / "wt", _ref())


def test_find_fails_closed_when_a_lens_answers_garbage(tmp_path: Path) -> None:
    """A lens whose answer never validates leaves its findings unknown: escalate."""
    backend = _FakeBackend(
        answers={"**correctness**": _findings_json("c-1"), "**contract**": "no idea"},
        default=_findings_json(),
    )
    pipe = _pipeline(tmp_path, backend)
    with pytest.raises(GateInfraError, match="invalid"):
        pipe.find(tmp_path / "wt", _ref())


def test_find_caps_at_max_candidates(tmp_path: Path) -> None:
    many = {
        "findings": [
            {
                "id": f"c-{i}",
                "file": f"a/mod{i}.py",
                "line": i,
                "claim": f"unique defect number {i}",
                "repro": "r",
                "testable": True,
            }
            for i in range(30)
        ]
    }
    backend = _FakeBackend(default=json.dumps(many))
    pipe = _pipeline(tmp_path, backend, config=_config(max_candidates=5))
    assert len(pipe.find(tmp_path / "wt", _ref())) == 5


def test_find_tolerates_an_empty_findings_list(tmp_path: Path) -> None:
    """No blockers is the normal, good outcome — it must not look like a failure."""
    backend = _FakeBackend(default=json.dumps({"findings": []}))
    pipe = _pipeline(tmp_path, backend)
    assert pipe.find(tmp_path / "wt", _ref()) == []


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_metrics_carry_the_full_funnel(tmp_path: Path) -> None:
    pipe = _pipeline(tmp_path, _FakeBackend())
    pipe.evidence.confirmed.append({"id": "a", "source": "lens", "test_file": "t.py"})
    pipe.evidence.confirmed.append({"id": "b", "source": "judge-untestable", "test_file": None})
    pipe.evidence.untestable.append(_finding("c-1").to_dict())
    pipe.evidence.rejected = 4
    pipe._candidates = [f.to_dict() for f in [_finding("a"), _finding("b")]]

    metric = pipe._metrics(
        gm.RoundMetric(0, "abc123", 2),
        _ref(),
        outcome=gm.OUTCOME_STALLED,
        rounds=[gm.RoundMetric(0, "abc123", 2), gm.RoundMetric(1, "def456", 2, fixed=0)],
        head="def456",
    )
    assert metric.candidates == 2
    assert metric.confirmed == 2
    assert metric.rejected == 4
    assert metric.untestable == 1
    assert metric.untestable_real == 1
    assert metric.failing_by_round == [2, 2]
    assert metric.outcome == gm.OUTCOME_STALLED
    assert metric.head_sha == "def456"


def test_lens_marked_untestable_claims_reach_the_judge(tmp_path: Path) -> None:
    """A finding the lens marked testable=false must not be silently dropped."""
    pipe = _pipeline(tmp_path, _FakeBackend())
    pipe.verify(tmp_path / "wt", [_finding("u-1", testable=False)], source="lens")
    assert [c["id"] for c in pipe.evidence.untestable] == ["u-1"]


def test_finish_keeps_the_converge_round_trace(tmp_path: Path) -> None:
    """_finish must record converge()'s per-round metrics, not a synthetic round 0."""
    from agent_fleet.contracts.gate import GateOutcome

    pipe = _pipeline(tmp_path, _FakeBackend())
    rounds = [
        gm.RoundMetric(round=0, head="aaa", failing=3),
        gm.RoundMetric(round=1, head="bbb", failing=0),
    ]
    metric = gm.GateMetrics(run_id="r", repo="x", pr=1, start_sha="aaa", rounds=rounds)
    result = pipe._finish(GateOutcome.APPROVED, "bbb", [], None, metric=metric)
    assert result.metrics is metric
    assert [r.failing for r in result.metrics.rounds] == [3, 0]
    assert result.metrics.outcome == gm.OUTCOME_CONVERGED


def test_verify_schema_accepts_a_null_test_file() -> None:
    import json as _json

    from agent_fleet.gate.pipeline import validate_verify

    validate_verify(_json.loads('{"verdict": "UNTESTABLE", "test_file": null, "reason": "r"}'))


def test_every_gate_role_runs_with_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression (false-negative gate): lens/verify/judge ran in plan mode — no git
    diff, no grep, no test writing — so real blockers were never found or proven."""
    from agent_fleet.gate import pipeline as pl

    modes: list[str | None] = []
    real = pl.call_structured

    def spy(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        modes.append(kwargs.get("mode"))
        return real(*args, **kwargs)

    monkeypatch.setattr(pl, "call_structured", spy)
    backend = _FakeBackend(default=_findings_json())
    pipe = _pipeline(tmp_path, backend)
    pipe.find(tmp_path / "wt", _ref())
    assert modes and all(m == "agent" for m in modes), modes
