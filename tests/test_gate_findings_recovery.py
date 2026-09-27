"""Regression tests: the gate was losing lens findings on the way to ``candidates=0``.

Every test here reproduced a real defect found in the 2026-09-25 pilot on
lake-of-rage PR #3541, where all four lenses reported **0 parsed findings** even
running with tools on — while the old bash gate found and confirmed three real
blockers on the same PR. Each test names the mechanism in its docstring, because
a silent ``candidates=0`` is indistinguishable from a clean PR unless the
mechanism that produced it is pinned down.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 - built into concrete paths at runtime
from typing import Any

import pytest

from agent_fleet.contracts.gate import GateOutcome, validate_findings
from agent_fleet.gate.config import GateConfig
from agent_fleet.gate.pipeline import GatePipeline
from agent_fleet.gate.structured import _first_valid, call_structured, json_candidates
from agent_fleet.model_policy import ModelPolicy

# A realistic lens answer: prose, a copy of the schema template, then the real
# findings. This is the shape observed from the pilot run.
_LENS_ANSWER = """I reviewed the diff for the `has_shadowless` helper and the reconcile
rescore path.

Following the required format, my answer is:

```json
{"findings": []}
```

That was the empty template. After tracing the actual code I found three
blockers, so the real answer is:

```json
{"findings": [{"id": "correctness-1", "file": "pipe/gold/build_rollup.py",
"line": 412, "claim": "has_shadowless crashes when shadowless is null",
"repro": "null shadowless -> AttributeError instead of a False default",
"testable": true}]}
```
"""


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Result:
    stdout: str
    stderr: str = ""
    exit_code: int = 0
    duration_s: float = 0.0
    agent_id: str | None = None
    usage: dict[str, int] | None = None


@dataclass
class _ScriptedBackend:
    answers: dict[str, str] = field(default_factory=dict)
    default: str = ""
    prompts: list[str] = field(default_factory=list)
    calls: int = 0

    def run(self, prompt: str, **_kwargs: Any) -> _Result:  # noqa: ANN401
        self.prompts.append(prompt)
        self.calls += 1
        for needle, answer in self.answers.items():
            if needle in prompt:
                return _Result(answer)
        return _Result(self.default)


def _pipeline(
    tmp_path: Path, backend: _ScriptedBackend, *, enable_fix: bool = False
) -> GatePipeline:
    config = GateConfig(
        backend="cmd",
        model="m",
        judge_backend="cmd",
        judge_model="m",
        enable_judge=False,
        enable_fix=enable_fix,
        max_fix_rounds=4,
        lens_timeout_s=10,
        verify_timeout_s=10,
        judge_timeout_s=10,
        fix_timeout_s=10,
        test_timeout_s=10,
    )
    return GatePipeline(
        repo=tmp_path / "repo",
        pr_number=3541,
        config=config,
        policy=ModelPolicy(backends={}),
        backend=backend,  # type: ignore[arg-type]
        gate_dir=tmp_path / "gate",
        use_systemd=False,
    )


def _stub_converge_git(monkeypatch: pytest.MonkeyPatch, *, head: str) -> None:
    """Let converge() reach a fix round: no real repo, no real pytest, no gh.

    The fixer is scripted to report a push, so the round is scored; the
    untestable verdict is left to the caller to stub on ``recheck_untestable``.
    """
    monkeypatch.setattr("agent_fleet.gate.pipeline.prepare_worktree", lambda *_a, **_k: None)
    monkeypatch.setattr("agent_fleet.gate.pipeline.remove_worktree", lambda *_a, **_k: None)
    monkeypatch.setattr("agent_fleet.gate.pipeline.fetch_base", lambda *_a, **_k: None)
    monkeypatch.setattr("agent_fleet.gate.pipeline.current_pr_head", lambda *_a: head)


# ---------------------------------------------------------------------------
# Defect 1: a bare, unfenced answer outranked nothing — position was ignored
# ---------------------------------------------------------------------------


def test_a_later_bare_object_outranks_an_earlier_fenced_template() -> None:
    """json_candidates sorted by *kind* (fenced first), not by position in the text.

    A lens that first echoes the schema template in a fence and then answers
    with an unfenced object had its template returned and its findings dropped.
    """
    text = (
        'My answer follows the format:\n```json\n{"findings": []}\n```\n'
        "But the real answer is:\n"
        '{"findings": [{"id": "c-1", "file": "a.py", "line": 4, "claim": "real",'
        ' "repro": "r", "testable": true}]}'
    )
    candidates = json_candidates(text)
    assert candidates[0]["findings"][0]["claim"] == "real"


def test_first_valid_picks_the_real_findings_not_the_empty_template() -> None:
    """End to end through the validator: 1 finding, not 0."""
    data, error = _first_valid(_LENS_ANSWER, validate_findings)
    assert data is not None, error
    assert len(data["findings"]) == 1
    assert data["findings"][0]["id"] == "correctness-1"


def test_first_valid_still_prefers_the_final_fenced_block() -> None:
    """Ordering by position keeps the existing 'last block wins' contract."""
    text = (
        '```json\n{"verdict": "OLD"}\n```\nthen corrected:\n```json\n{"verdict": "CONFIRMED"}\n```'
    )
    data, _error = _first_valid(text, _requires_verdict)
    assert data == {"verdict": "CONFIRMED"}


# ---------------------------------------------------------------------------
# Defect 2: a bare JSON array answer was invisible to the parser
# ---------------------------------------------------------------------------


def test_a_bare_findings_array_is_recovered() -> None:
    """A lens that answers `[...]` instead of `{"findings": [...]}` yielded nothing.

    ``json_candidates`` only walked balanced ``{...}`` objects, so a list answer
    parsed to zero candidates and read as a clean review.
    """
    text = (
        "Here are the blockers:\n"
        '[{"id": "c-1", "file": "a.py", "line": 4, "claim": "real",'
        ' "repro": "r", "testable": true}]'
    )
    data, error = _first_valid(text, validate_findings, list_key="findings")
    assert data is not None, error
    assert data["findings"][0]["id"] == "c-1"


def test_a_bare_array_does_not_outrank_a_later_well_formed_object() -> None:
    """Position ordering still applies once arrays are candidates too."""
    text = (
        'draft: [{"id": "draft-1", "file": "a.py", "line": 1, "claim": "draft",'
        ' "repro": "r", "testable": true}]\n'
        "final answer:\n"
        '{"findings": [{"id": "final-1", "file": "a.py", "line": 2, "claim": "final",'
        ' "repro": "r", "testable": true}]}'
    )
    data, _error = _first_valid(text, validate_findings, list_key="findings")
    assert data is not None
    assert data["findings"][0]["id"] == "final-1"


def test_call_structured_recovers_a_bare_array_lens_answer(tmp_path: Path) -> None:
    """The full call path: a list answer becomes findings, not a fail-closed raise."""
    backend = _ScriptedBackend(
        default='[{"id": "c-1", "file": "a.py", "line": 1, "claim": "x", "repro": "y",'
        ' "testable": true}]'
    )
    answer = call_structured(
        backend,  # type: ignore[arg-type]
        "prompt",
        model="m",
        cwd=tmp_path,
        timeout_s=10,
        validate=validate_findings,
        list_key="findings",
    )
    assert answer.data["findings"][0]["id"] == "c-1"
    assert backend.calls == 1  # recovered without burning a retry


# ---------------------------------------------------------------------------
# Defect 3: the lens stage lost findings even when parsing worked
# ---------------------------------------------------------------------------


def test_find_recovers_findings_from_a_template_then_answer_lens(tmp_path: Path) -> None:
    """The full lens stage on the pilot's answer shape: 1 candidate, not 0."""
    backend = _ScriptedBackend(default=_LENS_ANSWER)
    pipe = _pipeline(tmp_path, backend)
    found = pipe.find(tmp_path / "wt", _ref())
    assert len(found) == 1
    assert found[0].id == "correctness-1"


def test_the_lens_prompt_names_the_resolved_diff_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lens must be told the ref to diff against, and run it in the worktree.

    A stale local ``main`` is how a lens ends up reviewing the whole repo (or
    nothing): the prompt must carry the resolved ``origin/<base>`` and the call
    must run with the PR worktree as cwd.
    """
    backend = _ScriptedBackend(default=json.dumps({"findings": []}))
    pipe = _pipeline(tmp_path, backend)
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True)
    monkeypatch.setattr(
        "agent_fleet.gate.pipeline.resolve_diff_base",
        lambda *_a, **_k: "origin/main",
    )

    seen: list[tuple[str, Any]] = []
    import agent_fleet.gate.pipeline as pl

    real = pl.call_structured

    def spy(backend_obj: Any, prompt: str, **kwargs: Any) -> Any:  # noqa: ANN401 - mirrors call_structured's passthrough
        seen.append((prompt, kwargs.get("cwd")))
        return real(backend_obj, prompt, **kwargs)

    monkeypatch.setattr(pl, "call_structured", spy)
    pipe.find(worktree, _ref())

    assert seen
    assert len(seen) == len(pipe.config.lenses)
    for prompt, cwd in seen:
        assert "git diff origin/main...HEAD" in prompt
        assert cwd == worktree  # the PR worktree, not the main checkout


# ---------------------------------------------------------------------------
# Defect 4: a turn-cap run reported empty, reading as a clean review
# ---------------------------------------------------------------------------


def test_a_turn_cap_result_does_not_erase_the_answer() -> None:
    """``_parse_cmd_stream`` overwrote the accumulated text with an empty finalText.

    ``cmd`` exits 8 on the turn cap with a ``result`` event whose ``finalText`` is
    empty. That wiped the assistant text carrying the findings JSON, so a lens
    that HAD found blockers returned nothing.
    """
    from agent_fleet.cmd_backend import _parse_cmd_stream

    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "event",
                    "event": {
                        "type": "assistant",
                        "text": '```json\n{"findings": [{"id": "c-1", "file": "a.py",'
                        ' "line": 1, "claim": "real", "repro": "r", "testable": true}]}\n```',
                    },
                }
            ),
            json.dumps({"type": "result", "subtype": "error_max_turns", "finalText": ""}),
        ]
    )
    final, _sid, _usage = _parse_cmd_stream(stdout, "")
    assert "findings" in final, "the turn-cap answer was erased"


def test_an_unchipped_result_still_wins() -> None:
    """A real finalText keeps priority over accumulated assistant text."""
    from agent_fleet.cmd_backend import _parse_cmd_stream

    stdout = "\n".join(
        [
            json.dumps({"type": "event", "event": {"type": "assistant", "text": "thinking"}}),
            json.dumps({"type": "result", "finalText": '{"findings": []}'}),
        ]
    )
    final, _sid, _usage = _parse_cmd_stream(stdout, "")
    assert final == '{"findings": []}'


# ---------------------------------------------------------------------------
# Defect 5: a turn-capped reviewer was recorded as a completed, clean answer
# ---------------------------------------------------------------------------


def test_a_turn_capped_lens_is_not_a_clean_review(tmp_path: Path) -> None:
    """A reviewer that ran out of turns never reached a verdict.

    Measured on the live #3541 re-run: every lens used all 80 turns of
    investigation and never wrote its final JSON, exited 8, and the gate read
    the *repair* turn's ``{"findings": []}`` as its answer. The pipeline must
    fail closed, exactly as it does for a dead agent.
    """
    from agent_fleet.gate.pipeline import GateInfraError

    class _TurnCapped(_ScriptedBackend):
        """Burns the turn budget investigating, never writes its verdict."""

        def run(self, prompt: str, **_kwargs: Any) -> _Result:  # noqa: ANN401
            self.prompts.append(prompt)
            self.calls += 1
            if "not in the required format" in prompt:
                # The repair turn: "keep every finding you made" -> nothing found.
                return _Result('```json\n{"findings": []}\n```')
            return _Result(
                "I examined the diff and traced the call sites. Let me keep digging.",
                exit_code=8,
            )

    pipe = _pipeline(tmp_path, _TurnCapped())
    with pytest.raises(GateInfraError):
        pipe.find(tmp_path / "wt", _ref())


def test_a_turn_capped_call_is_recorded_with_its_exit_code(tmp_path: Path) -> None:
    """The persisted record must show the turn cap, not a clean parsed answer."""
    from agent_fleet.gate.pipeline import GateInfraError

    class _TurnCapped(_ScriptedBackend):
        def run(self, prompt: str, **_kwargs: Any) -> _Result:  # noqa: ANN401
            self.prompts.append(prompt)
            self.calls += 1
            if "not in the required format" in prompt:
                return _Result('```json\n{"findings": []}\n```')
            return _Result("still investigating", exit_code=8)

    pipe = _pipeline(tmp_path, _TurnCapped())
    with contextlib.suppress(GateInfraError):
        pipe.find(tmp_path / "wt", _ref())

    records = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((tmp_path / "gate" / "calls").glob("*.json"))
    ]
    assert records
    assert all(r["exit_code"] == 8 for r in records), records
    assert all(r["parsed_ok"] is False for r in records)


def test_the_cmd_backend_passes_the_turn_cap_exit_through(tmp_path: Path) -> None:
    """``CmdBackend.run`` flattened exit 8 into 0, hiding the cap from callers."""
    from unittest.mock import MagicMock, patch

    from agent_fleet.cmd_backend import CmdBackend

    def _run(_cmd: list[str], **_kwargs: object) -> MagicMock:
        m = MagicMock()
        m.returncode = 8  # the turn cap
        m.stdout = json.dumps({"type": "result", "finalText": "partial"})
        m.stderr = ""
        return m

    with (
        patch("agent_fleet.cmd_backend.subprocess.run", side_effect=_run),
        patch("agent_fleet.cmd_backend.check_cmd_auth", return_value=(True, "ok", "")),
    ):
        result = CmdBackend(cmd_bin="/bin/cmd").run(
            "review", max_tokens=0, timeout_s=10, cwd=tmp_path
        )
    assert result.exit_code == 8


def test_a_completed_lens_answer_is_unaffected(tmp_path: Path) -> None:
    """A reviewer that finished on time must still be accepted."""
    backend = _ScriptedBackend(default='```json\n{"findings": []}\n```')
    answer = call_structured(
        backend,  # type: ignore[arg-type]
        "prompt",
        model="m",
        cwd=tmp_path,
        timeout_s=10,
        validate=validate_findings,
        list_key="findings",
    )
    assert answer.data == {"findings": []}
    assert backend.calls == 1


# ---------------------------------------------------------------------------
# Defect 6: the run reported "cap after 1 round(s)" for a PR it never tried to fix
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _green_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every pytest in the run is green, and worktree creation is stubbed out.

    Autouse: the convergence assertions in this file are all about what the
    gate does when the deterministic half is already green, and the lens-stage
    tests never reach the test runner at all.
    """
    from agent_fleet.gate.pipeline import TestRun

    monkeypatch.setattr("agent_fleet.gate.pipeline.prepare_worktree", lambda *_a, **_k: None)
    monkeypatch.setattr("agent_fleet.gate.pipeline.remove_worktree", lambda *_a, **_k: None)
    monkeypatch.setattr("agent_fleet.gate.pipeline.fetch_base", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "agent_fleet.gate.pipeline.GateTestRunner.run",
        lambda _self, _files: TestRun(failing=[], ran=1),
    )


def _with_untestable_blocker(pipe: GatePipeline) -> None:
    pipe.evidence.confirmed.append(
        {
            "id": "u-1",
            "source": "judge-untestable",
            "claim": "reconcile erases a fence",
            "test_file": None,
        }
    )


def test_untestable_only_blockers_get_one_fix_round_not_a_bare_escalation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """failing==0 with only untestable_real blockers must not say 'cap after 1 round(s)'.

    The fixer was dispatched to a PR whose entire test set was green, and the
    resulting "cap" reason read as "we tried and ran out of rounds". It now gets
    one round carrying the untestable list, after which the recheck judge — not
    the failing-set maths — decides.
    """
    from agent_fleet.gate import metrics as gm

    backend = _ScriptedBackend()
    pipe = _pipeline(tmp_path, backend, enable_fix=True)
    _with_untestable_blocker(pipe)
    _stub_converge_git(monkeypatch, head="a" * 40)
    monkeypatch.setattr(GatePipeline, "recheck_untestable", lambda *_a, **_k: False)

    _head, metric = pipe.converge(ref=_ref(), pr_tests=[])
    assert metric.outcome == gm.OUTCOME_UNTESTABLE_NEEDS_REVIEW
    assert metric.round_count == 2  # baseline + the one untestable round
    assert len(backend.prompts) == 1, "the untestable blocker never reached a fixer"
    assert "reconcile erases a fence" in backend.prompts[0]
    assert metric.untestable_real == 1


def test_the_untestable_needs_review_reason_names_the_blockers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The escalation reason must say a human has to look, and at what."""
    from agent_fleet.gate import metrics as gm
    from agent_fleet.gate.pipeline import untestable_review_reason

    pipe = _pipeline(tmp_path, _ScriptedBackend(), enable_fix=True)
    _with_untestable_blocker(pipe)
    _stub_converge_git(monkeypatch, head="a" * 40)
    monkeypatch.setattr(GatePipeline, "recheck_untestable", lambda *_a, **_k: False)
    _head, metric = pipe.converge(ref=_ref(), pr_tests=[])

    reason = untestable_review_reason(pipe.evidence.confirmed)
    assert reason is not None
    assert "untestable blocker(s) need human review" in reason
    assert "reconcile erases a fence" in reason
    assert metric.outcome == gm.OUTCOME_UNTESTABLE_NEEDS_REVIEW


def test_a_green_pr_with_no_blockers_still_converges(tmp_path: Path) -> None:
    """The new early exit must not swallow the ordinary approval path."""
    from agent_fleet.gate import metrics as gm

    pipe = _pipeline(tmp_path, _ScriptedBackend(), enable_fix=True)
    _head, metric = pipe.converge(ref=_ref(), pr_tests=[])
    assert metric.outcome == gm.OUTCOME_CONVERGED


# ---------------------------------------------------------------------------
# Defect 6: nothing was persisted, so the loss could not be traced after the fact
# ---------------------------------------------------------------------------


def test_every_lens_call_is_persisted_with_its_raw_text_and_parse_state(
    tmp_path: Path,
) -> None:
    """Each lens/verify/judge call must leave a record under ``<run_dir>/calls/``."""
    backend = _ScriptedBackend(default=_LENS_ANSWER)
    pipe = _pipeline(tmp_path, backend)
    pipe.find(tmp_path / "wt", _ref())

    calls_dir = tmp_path / "gate" / "calls"
    files = sorted(p.name for p in calls_dir.glob("*.json"))
    assert len(files) == len(pipe.config.lenses)
    assert all(name.startswith("lens-") for name in files)

    for name in files:
        record = json.loads((calls_dir / name).read_text(encoding="utf-8"))
        assert record["raw"] == _LENS_ANSWER
        assert record["exit_code"] == 0
        assert record["parsed_ok"] is True
        assert record["n_items"] == 1
        assert record["parse_error"] == ""
        assert record["duration_s"] >= 0.0
        assert record["model"]


def test_a_failing_lens_call_is_still_persisted(tmp_path: Path) -> None:
    """A dead lens is exactly when the raw text matters most."""
    from agent_fleet.gate.pipeline import GateInfraError

    backend = _ScriptedBackend(default="")
    pipe = _pipeline(tmp_path, backend)

    with contextlib.suppress(GateInfraError):
        pipe.find(tmp_path / "wt", _ref())

    files = sorted((tmp_path / "gate" / "calls").glob("*.json"))
    assert files, "a failed lens call left no record"
    for path in files:
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["parsed_ok"] is False
        assert record["parse_error"]
        assert record["exit_code"] != 0 or record["raw"] == ""


def test_the_gate_result_reports_the_per_lens_funnel(tmp_path: Path) -> None:
    """GateResult must carry raw_len / parsed_ok / n_items / error per lens."""
    backend = _ScriptedBackend(default=_LENS_ANSWER)
    pipe = _pipeline(tmp_path, backend)
    pipe.find(tmp_path / "wt", _ref())

    result = pipe._result_for(GateOutcome.NEEDS_ESCALATION, "", ["candidates found"], _ref())
    per_lens = result.funnel()["lens_calls"]
    assert len(per_lens) == len(pipe.config.lenses)
    for row in per_lens:
        assert row["parsed_ok"] is True
        assert row["n_items"] == 1
        assert row["raw_len"] == len(_LENS_ANSWER)
        assert row["parse_error"] == ""
        assert row["lens"]
    assert result.to_dict()["funnel"]["lens_calls"] == per_lens


def _requires_verdict(data: dict[str, Any]) -> None:
    if "verdict" not in data:
        raise ValueError("missing verdict")


def _ref() -> Any:  # noqa: ANN401
    from agent_fleet.gate.gitops import PullRequestRef

    return PullRequestRef(number=3541, head_ref="fb/lane", head_sha="b" * 40, state="OPEN")


# ---------------------------------------------------------------------------
# The turn budget itself
# ---------------------------------------------------------------------------


def test_the_gate_lens_turn_budget_is_not_the_old_80() -> None:
    """80 turns was exhausted mid-review; a gate lens needs a real budget.

    Measured: all four lenses on #3541 used every one of 80 turns on
    investigation and stopped before writing their JSON.
    """
    from agent_fleet.cmd_backend import DEFAULT_MAX_TURNS

    assert DEFAULT_MAX_TURNS >= 200


def test_the_repair_turn_warns_against_an_empty_list() -> None:
    """An empty findings list is a claim of cleanliness, not a shrug.

    The turn-capped lenses answered their repair turn with ``{"findings": []}``,
    which asserted the PR was clean when the reviewer had actually been cut off
    mid-investigation. The repair turn must say so.
    """
    from agent_fleet.gate.structured import _repair_prompt

    prompt = _repair_prompt("original", "still investigating", "no JSON found")
    assert "an empty findings list asserts the change is clean" in prompt
