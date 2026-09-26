"""Per-stage timeouts, and a stage timeout is a dead agent.

One ``agent_timeout_s`` (30 min) drove lens, verify and fix, while the
judge got a separate 7200s. Those numbers were wrong in both directions:
a fixer that commits, pushes and waits on a test suite has no chance in
30 minutes, and a lens that is going to produce nothing is allowed a
full half hour before anyone notices.

Per-stage budgets fix that. But a timeout must also *fail closed*: an
agent that ran out of time produced no evidence either way, and reading
that as "no findings" is exactly the bug class that once let a
turn-capped reviewer read as a clean review and approve a PR with three
real blockers.

So: per-stage defaults of 40 min for lens/verify/judge and 90 min for
fix, ``agent_timeout_s`` honoured as a deprecated alias so no existing
fleet.yaml silently loses its budget, and a timeout reported as a dead
agent naming the stage and how long it ran.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from agent_fleet.gate import metrics as gm
from agent_fleet.gate.config import GateConfig, load_gate_config
from agent_fleet.gate.pipeline import GateInfraError, GatePipeline
from agent_fleet.gate.structured import TIMEOUT_EXIT, call_structured
from agent_fleet.model_policy import ModelPolicy

_LENS_ANSWER = (
    '```json\n{"findings": []}\n```',
    '```json\n{"untestable_rulings": [], "new_blockers": []}\n```',
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_config_defaults_match_the_documented_stage_timeouts() -> None:
    cfg = GateConfig()
    assert cfg.lens_timeout_s == 2400  # 40 min
    assert cfg.verify_timeout_s == 2400
    assert cfg.judge_timeout_s == 2400
    assert cfg.fix_timeout_s == 5400  # 90 min: commit + push + a test suite


def test_each_stage_gets_its_own_budget() -> None:
    """The old design gave every non-judge stage one shared number; that is
    exactly the coupling that made the fix budget unusable."""
    cfg = GateConfig(lens_timeout_s=11, verify_timeout_s=22, judge_timeout_s=33, fix_timeout_s=44)
    assert cfg.stage_timeout("lens") == 11
    assert cfg.stage_timeout("verify") == 22
    assert cfg.stage_timeout("judge") == 33
    assert cfg.stage_timeout("fix") == 44


def test_an_unknown_stage_falls_back_to_the_review_budget() -> None:
    """Better a too-small budget for an unmapped role than a crash mid-run."""
    assert GateConfig().stage_timeout("not-a-role") == GateConfig().lens_timeout_s


def test_stage_timeouts_load_from_config() -> None:
    cfg = load_gate_config({"gate": {"lens_timeout_s": 111, "fix_timeout_s": 222}})
    assert cfg.lens_timeout_s == 111
    assert cfg.fix_timeout_s == 222
    assert cfg.judge_timeout_s == 2400  # untouched default


def test_legacy_agent_timeout_s_is_still_applied_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A fleet.yaml written before per-stage budgets must not silently lose them."""
    with caplog.at_level(logging.WARNING, logger="agent_fleet.gate.config"):
        cfg = load_gate_config({"gate": {"agent_timeout_s": 1800}})
    assert cfg.lens_timeout_s == 1800
    assert cfg.verify_timeout_s == 1800
    assert cfg.fix_timeout_s == 1800
    assert any("agent_timeout_s" in r.message for r in caplog.records)


def test_an_explicit_stage_key_wins_over_the_legacy_alias() -> None:
    cfg = load_gate_config({"gate": {"agent_timeout_s": 1800, "fix_timeout_s": 5400}})
    assert cfg.fix_timeout_s == 5400
    assert cfg.lens_timeout_s == 1800


def test_the_judge_keeps_its_own_default_rather_than_inheriting_the_legacy_key() -> None:
    """The legacy key never drove the judge, so it must not start doing so."""
    assert load_gate_config({"gate": {"agent_timeout_s": 1800}}).judge_timeout_s == 2400


# ---------------------------------------------------------------------------
# A timeout is classified as a timeout, not a crash
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, exit_code: int, stdout: str = "") -> None:
        self.stdout = stdout
        self.stderr = ""
        self.exit_code = exit_code
        self.duration_s = 12.0


class _FixedExitBackend:
    """A backend that always exits with one code and prints nothing."""

    def __init__(self, exit_code: int, stdout: str = "") -> None:
        self._exit_code = exit_code
        self._stdout = stdout

    def run(self, *_args: object, **_kwargs: object) -> _Result:
        return _Result(self._exit_code, self._stdout)


def test_exit_124_is_classified_as_a_timeout_not_a_crash(tmp_path: Path) -> None:
    """124 is the shell's `timeout` signal; treating it as a generic crash
    loses the one fact the operator needs: which stage ran out of budget."""
    from agent_fleet.gate.structured import StructuredCallError

    with pytest.raises(StructuredCallError) as exc:
        call_structured(
            _FixedExitBackend(TIMEOUT_EXIT),  # type: ignore[arg-type]
            "prompt",
            model="m",
            cwd=Path(tmp_path),
            timeout_s=600,
            validate=lambda _d: None,
        )
    assert exc.value.kind == "timeout"
    assert "600" in str(exc.value)


def test_a_non_timeout_crash_is_still_just_dead(tmp_path: Path) -> None:
    from agent_fleet.gate.structured import StructuredCallError

    with pytest.raises(StructuredCallError) as exc:
        call_structured(
            _FixedExitBackend(1),  # type: ignore[arg-type]
            "prompt",
            model="m",
            cwd=Path(tmp_path),
            timeout_s=600,
            validate=lambda _d: None,
        )
    assert exc.value.kind == "dead"


# ---------------------------------------------------------------------------
# End to end: a timed-out stage escalates, naming the stage and the elapsed time
# ---------------------------------------------------------------------------


def _pipeline(tmp_path: Path, backend: object, *, enable_judge: bool = False) -> GatePipeline:
    config = GateConfig(
        backend="cmd",
        model="m",
        judge_backend="cmd",
        judge_model="m",
        enable_judge=enable_judge,
        enable_fix=False,
        lens_timeout_s=600,
        verify_timeout_s=600,
        judge_timeout_s=600,
        fix_timeout_s=600,
    )
    return GatePipeline(
        repo=tmp_path / "repo",
        pr_number=7,
        config=config,
        policy=ModelPolicy(backends={}),
        backend=backend,  # type: ignore[arg-type]
        judge_backend=backend if enable_judge else None,  # type: ignore[arg-type]
        gate_dir=tmp_path / "gate",
        use_systemd=False,
        lane_slug="fb/lane",
    )


def _ref() -> Any:  # noqa: ANN401
    from agent_fleet.gate.gitops import PullRequestRef

    return PullRequestRef(number=7, head_ref="fb/lane", head_sha="a" * 40, state="OPEN")


def test_a_lens_timeout_fails_closed_naming_the_stage_and_elapsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agent_fleet.gate.pipeline.resolve_diff_base", lambda *_a: "origin/main")
    pipe = _pipeline(tmp_path, _FixedExitBackend(TIMEOUT_EXIT))
    with pytest.raises(GateInfraError) as exc:
        pipe.find(tmp_path / "wt", _ref())
    message = str(exc.value)
    assert "lens" in message
    assert "timed out" in message
    assert "s" in message  # the elapsed seconds are in the reason


def test_a_timeout_is_never_read_as_a_clean_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug class: a dead lens reporting nothing must not equal a clean PR.

    A timeout produced `candidates=0` on a PR with real blockers, which is
    indistinguishable from a clean review. The run must escalate instead.
    """
    monkeypatch.setattr("agent_fleet.gate.pipeline.resolve_diff_base", lambda *_a: "origin/main")
    pipe = _pipeline(tmp_path, _FixedExitBackend(TIMEOUT_EXIT))
    with pytest.raises(GateInfraError):
        pipe.find(tmp_path / "wt", _ref())
    assert pipe._candidates == []  # nothing was collected, and nothing was claimed


def test_a_judge_timeout_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_fleet.gate.pipeline.resolve_diff_base", lambda *_a: "origin/main")
    pipe = _pipeline(tmp_path, _FixedExitBackend(TIMEOUT_EXIT), enable_judge=True)
    with pytest.raises(GateInfraError) as exc:
        pipe.judge(tmp_path / "wt", _ref())
    assert "judge" in str(exc.value)


def test_a_fix_round_timeout_fails_closed_naming_the_stage(tmp_path: Path) -> None:
    """The fixer has no JSON schema, so a timeout there is only visible from
    the backend's exit code — without this it is logged and the round carries
    on as if the fixer had merely pushed nothing."""
    pipe = _pipeline(tmp_path, _FixedExitBackend(TIMEOUT_EXIT))
    with pytest.raises(GateInfraError) as exc:
        pipe._run_fixer("prompt", model="m", cwd=tmp_path)
    message = str(exc.value)
    assert "fix" in message
    assert "timed out" in message


# ---------------------------------------------------------------------------
# The budget a stage is actually given
# ---------------------------------------------------------------------------


def test_the_fixer_is_given_the_fix_budget_not_the_lens_one(tmp_path: Path) -> None:
    seen: list[int] = []

    class _Backend:
        def run(self, _prompt: str, **kwargs: Any) -> Any:  # noqa: ANN401
            seen.append(int(kwargs["timeout_s"]))
            return _Result(0)

    pipe = _pipeline(tmp_path, _Backend())
    pipe._run_fixer("prompt", model="m", cwd=tmp_path)
    assert seen == [600]


def test_the_judge_is_given_the_judge_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int] = []

    class _Backend:
        def run(self, _prompt: str, **kwargs: Any) -> Any:  # noqa: ANN401
            seen.append(int(kwargs["timeout_s"]))
            return _Result(0, _LENS_ANSWER[1])

    monkeypatch.setattr("agent_fleet.gate.pipeline.resolve_diff_base", lambda *_a: "origin/main")
    pipe = _pipeline(tmp_path, _Backend(), enable_judge=True)
    pipe.judge(tmp_path / "wt", _ref())
    assert seen == [600]


def test_a_timeout_run_is_recorded_in_metrics_as_a_dead_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The escalation reason must survive into the call records; a dead stage
    that left no trace is the failure mode the call recorder exists to stop."""
    monkeypatch.setattr("agent_fleet.gate.pipeline.resolve_diff_base", lambda *_a: "origin/main")
    pipe = _pipeline(tmp_path, _FixedExitBackend(TIMEOUT_EXIT))
    with pytest.raises(GateInfraError):
        pipe.find(tmp_path / "wt", _ref())
    rows = pipe.recorder.rows()
    assert rows, "a dead stage left no record"
    assert {r["stage"] for r in rows} == {"lens"}
    assert all(r["exit_code"] == TIMEOUT_EXIT for r in rows)
    assert all(r["parsed_ok"] is False for r in rows)
    assert all("timed out" in r["parse_error"] for r in rows)


def test_the_gate_reports_a_timeout_escalation_not_an_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full run() path: a timed-out lens must land on NEEDS_ESCALATION."""
    from agent_fleet.contracts.gate import GateOutcome
    from agent_fleet.gate.gitops import PullRequestRef

    ref = PullRequestRef(number=7, head_ref="fb/lane", head_sha="a" * 40, state="OPEN")
    monkeypatch.setattr("agent_fleet.gate.pipeline.fetch_base", lambda *_a, **_k: None)
    monkeypatch.setattr("agent_fleet.gate.pipeline.resolve_pull_request", lambda *_a: ref)
    monkeypatch.setattr("agent_fleet.gate.pipeline.prepare_worktree", lambda *_a, **_k: None)
    monkeypatch.setattr("agent_fleet.gate.pipeline.remove_worktree", lambda *_a, **_k: None)
    monkeypatch.setattr("agent_fleet.gate.pipeline.changed_test_files", lambda *_a: [])
    monkeypatch.setattr("agent_fleet.gate.pipeline.resolve_diff_base", lambda *_a: "origin/main")

    pipe = _pipeline(tmp_path, _FixedExitBackend(TIMEOUT_EXIT))
    result = pipe.run()
    assert result.outcome is GateOutcome.NEEDS_ESCALATION
    assert result.approved is False
    assert "timed out" in " ".join(result.reasons)
    assert result.metrics is not None
    assert result.metrics.outcome != gm.OUTCOME_CONVERGED
