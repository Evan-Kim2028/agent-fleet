"""Tests for the gate's batched verification re-run.

Verifiers still run one-per-claim in parallel, but the pipeline re-runs every test
they wrote in ONE pytest invocation. The bar for confirming a claim does not
move: a claim is confirmed only when a test in its OWN file fails. These tests
pin that attribution, because it is the one thing batching could quietly relax —
one verifier's genuinely failing test would otherwise confirm a different, false
claim that merely shared the invocation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 - concrete paths are built at runtime
from typing import TYPE_CHECKING, Any

from agent_fleet.contracts.gate import Finding
from agent_fleet.gate.config import GateConfig, load_gate_config
from agent_fleet.gate.pipeline import GatePipeline, GateTestRunner, TestRun
from agent_fleet.model_policy import ModelPolicy

if TYPE_CHECKING:
    import pytest


@dataclass
class _Recorder:
    """Records every ``GateTestRunner.run`` call so batching is observable.

    The default result fails every requested file's own test, which is what a
    real pytest run over those files would report.
    """

    calls: list[list[str]] = field(default_factory=list)
    passing: list[str] = field(default_factory=list)

    def __call__(self, files: list[str]) -> TestRun:
        self.calls.append(list(files))
        failing = [f"{f}::test_x" for f in files if f not in self.passing]
        return TestRun(failing=failing, ran=1, tests_failed=bool(failing))


@dataclass
class _Backend:
    """Answers a verify call by the finding id embedded in the prompt's claim JSON."""

    verdicts: dict[str, str] = field(default_factory=dict)
    prompts: list[str] = field(default_factory=list)

    def run(self, prompt: str, **_kwargs: Any) -> Any:  # noqa: ANN401
        self.prompts.append(prompt)
        payload = json.loads(prompt.split("Claim (JSON):\n", 1)[1].split("\n\n", 1)[0])
        return _Result(self.verdicts.get(payload["id"], ""))

    def models(self) -> list[str]:
        return []


@dataclass(frozen=True)
class _Result:
    stdout: str
    stderr: str = ""
    exit_code: int = 0
    duration_s: float = 0.0
    agent_id: str | None = None
    usage: dict[str, int] | None = None


def _finding(fid: str = "c-1") -> Finding:
    return Finding(
        id=fid,
        lens="correctness",
        file="agent_fleet/gate/pipeline.py",
        line=10,
        claim=f"defect {fid}",
        repro="input -> wrong",
        testable=True,
    )


def _policy() -> ModelPolicy:
    return ModelPolicy(backends={})


def _pipeline(tmp_path: Path, backend: _Backend, **cfg: Any) -> GatePipeline:  # noqa: ANN401
    return GatePipeline(
        repo=tmp_path / "repo",
        pr_number=42,
        # The cache is exercised in test_gate_test_cache.py; these tests stub the
        # runner, so it stays off here.
        config=GateConfig(backend="cmd", model="m", **cfg),
        policy=_policy(),
        backend=backend,  # type: ignore[arg-type]
        judge_backend=None,
        gate_dir=tmp_path / "gate",
        use_systemd=False,
    )


def _worktree(tmp_path: Path, *tests: str) -> Path:
    wt = tmp_path / "wt"
    (wt / "tests").mkdir(parents=True, exist_ok=True)
    for rel in tests:
        target = wt / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def test_x():\n    assert False\n", encoding="utf-8")
    return wt


def _confirm(test_file: str) -> str:
    return json.dumps({"verdict": "CONFIRMED", "test_file": test_file, "reason": "boom"})


# ---------------------------------------------------------------------------
# One invocation, and attribution to each claim's own tests
# ---------------------------------------------------------------------------


def test_all_confirmed_tests_run_in_one_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three CONFIRMED claims, one pytest process instead of three."""
    files = ["tests/test_gate_a.py", "tests/test_gate_b.py", "tests/test_gate_c.py"]
    backend = _Backend(verdicts=dict(zip(("a-1", "b-1", "c-1"), map(_confirm, files), strict=True)))
    pipe = _pipeline(tmp_path, backend)
    wt = _worktree(tmp_path, *files)
    rec = _Recorder()
    monkeypatch.setattr(GateTestRunner, "run", rec)

    pipe.verify(wt, [_finding("a-1"), _finding("b-1"), _finding("c-1")], source="lens")

    assert len(rec.calls) == 1, f"expected one batched invocation, got {rec.calls}"
    assert sorted(rec.calls[0]) == sorted(files)
    assert len(pipe.evidence.confirmed) == 3


def test_a_claim_is_confirmed_only_by_its_own_failing_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other claim's test failed, so this claim is refuted, not confirmed."""
    good, falsey = "tests/test_gate_good.py", "tests/test_gate_false.py"
    backend = _Backend(verdicts={"g-1": _confirm(good), "f-1": _confirm(falsey)})
    pipe = _pipeline(tmp_path, backend)
    wt = _worktree(tmp_path, good, falsey)

    def run(_self: GateTestRunner, files: list[str]) -> TestRun:
        assert files == sorted([good, falsey])
        return TestRun(
            failing=[f"{good}::test_x"],  # only the honest claim's test fails
            ran=1,
            tests_failed=True,
        )

    monkeypatch.setattr(GateTestRunner, "run", run)
    pipe.verify(wt, [_finding("g-1"), _finding("f-1")], source="lens")

    confirmed = [c["id"] for c in pipe.evidence.confirmed]
    assert confirmed == ["g-1"]
    assert pipe.evidence.rejected == 1
    assert any("did not fail" in r["reason"] for r in pipe.evidence.rejected_items)
    # The refuting test must not linger to be counted as evidence next round.
    assert not (wt / falsey).exists()
    assert (wt / good).exists()


def test_a_passing_claim_is_discarded_even_when_a_sibling_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 1 from the batch says someone failed; it does not say who."""
    good, falsey = "tests/test_gate_good.py", "tests/test_gate_false.py"
    backend = _Backend(verdicts={"g-1": _confirm(good), "f-1": _confirm(falsey)})
    pipe = _pipeline(tmp_path, backend)
    wt = _worktree(tmp_path, good, falsey)

    def run(_self: GateTestRunner, _files: list[str]) -> TestRun:
        return TestRun(failing=[f"{good}::test_x"], ran=1, tests_failed=True)

    monkeypatch.setattr(GateTestRunner, "run", run)
    pipe.verify(wt, [_finding("g-1"), _finding("f-1")], source="lens")

    assert [c["id"] for c in pipe.evidence.confirmed] == ["g-1"]


def test_attribution_does_not_match_a_similarly_named_file() -> None:
    """``test_x.py.bak`` must never be credited to ``test_x.py``."""
    run = TestRun(failing=["tests/test_x.py.bak::test_q"], ran=1, tests_failed=True)
    assert run.failed_tests_in("tests/test_x.py") == []
    assert run.failed_tests_in("tests/test_x.py.bak") == ["tests/test_x.py.bak::test_q"]


def test_failed_tests_in_returns_only_that_files_ids() -> None:
    run = TestRun(failing=["a/t1.py::x", "a/t2.py::y", "a/t2.py::z"], ran=1, tests_failed=True)
    assert run.failed_tests_in("a/t2.py") == ["a/t2.py::y", "a/t2.py::z"]


# ---------------------------------------------------------------------------
# rc >= 2: fall back to per-file runs, same fail-closed bar
# ---------------------------------------------------------------------------


def test_collection_error_falls_back_to_one_run_per_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = ["tests/test_gate_a.py", "tests/test_gate_b.py"]
    backend = _Backend(verdicts={"a-1": _confirm(files[0]), "b-1": _confirm(files[1])})
    pipe = _pipeline(tmp_path, backend)
    wt = _worktree(tmp_path, *files)
    rec = _Recorder()
    monkeypatch.setattr(GateTestRunner, "run", rec)

    def run(_self: GateTestRunner, files_arg: list[str]) -> TestRun:
        rec(files_arg)
        if len(files_arg) > 1:
            return TestRun(infra_error="collection error", ran=1)
        return TestRun(failing=[f"{files_arg[0]}::test_x"], ran=1, tests_failed=True)

    monkeypatch.setattr(GateTestRunner, "run", run)
    pipe.verify(wt, [_finding("a-1"), _finding("b-1")], source="lens")

    assert rec.calls[0] == sorted(files), "the batch is tried first"
    assert rec.calls[1:] == [[files[0]], [files[1]]], "then each file on its own"
    assert len(pipe.evidence.confirmed) == 2


def test_a_claim_still_fails_closed_when_its_own_fallback_run_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken file must reject only itself, not poison the healthy claim."""
    good, broken = "tests/test_gate_good.py", "tests/test_gate_broken.py"
    backend = _Backend(verdicts={"g-1": _confirm(good), "b-1": _confirm(broken)})
    pipe = _pipeline(tmp_path, backend)
    wt = _worktree(tmp_path, good, broken)

    def run(_self: GateTestRunner, files_arg: list[str]) -> TestRun:
        if len(files_arg) > 1:
            return TestRun(infra_error="collection error", ran=1)
        if files_arg == [broken]:
            return TestRun(infra_error="ImportError in test", ran=1)
        return TestRun(failing=[f"{good}::test_x"], ran=1, tests_failed=True)

    monkeypatch.setattr(GateTestRunner, "run", run)
    pipe.verify(wt, [_finding("g-1"), _finding("b-1")], source="lens")

    assert [c["id"] for c in pipe.evidence.confirmed] == ["g-1"]
    assert pipe.evidence.rejected == 1
    assert "could not run" in pipe.evidence.rejected_items[0]["reason"]


# ---------------------------------------------------------------------------
# Non-CONFIRMED verdicts never reach the re-run
# ---------------------------------------------------------------------------


def test_rejected_and_untestable_verdicts_are_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _Backend(
        verdicts={
            "r-1": json.dumps({"verdict": "REJECTED", "reason": "code is correct"}),
            "u-1": json.dumps({"verdict": "UNTESTABLE", "reason": "needs prod data"}),
        }
    )
    pipe = _pipeline(tmp_path, backend)
    wt = _worktree(tmp_path)
    rec = _Recorder()
    monkeypatch.setattr(GateTestRunner, "run", rec)

    pipe.verify(wt, [_finding("r-1"), _finding("u-1")], source="lens")

    assert rec.calls == [], "only CONFIRMED claims are re-run"
    assert pipe.evidence.confirmed == []
    assert pipe.evidence.rejected == 1
    assert len(pipe.evidence.untestable) == 1


def test_no_testable_claims_launches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finding = Finding(
        id="c-1",
        lens="correctness",
        file="a.py",
        line=1,
        claim="needs prod data",
        repro="x",
        testable=False,
    )
    pipe = _pipeline(tmp_path, _Backend())
    rec = _Recorder()
    monkeypatch.setattr(GateTestRunner, "run", rec)

    pipe.verify(_worktree(tmp_path), [finding], source="lens")

    assert rec.calls == []
    assert len(pipe.evidence.untestable) == 1


# ---------------------------------------------------------------------------
# Config plumbing
# ---------------------------------------------------------------------------


def test_cache_defaults() -> None:
    cfg = GateConfig()
    assert cfg.enable_test_cache is True
    assert cfg.test_cache_dir == "~/.agent-fleet/cache/gate-tests"
    assert cfg.test_cache_ttl_s == 24 * 3600


def test_cache_keys_parse_from_yaml() -> None:
    cfg = load_gate_config(
        {
            "gate": {
                "enable_test_cache": False,
                "test_cache_dir": "/var/tmp/gate-cache",
                "test_cache_ttl_s": 600,
            }
        }
    )
    assert cfg is not None
    assert cfg.enable_test_cache is False
    assert cfg.test_cache_dir == "/var/tmp/gate-cache"
    assert cfg.test_cache_ttl_s == 600


def test_cache_can_be_turned_off_per_repo() -> None:
    cfg = load_gate_config({"gate": {"enable_test_cache": False}})
    assert cfg is not None and cfg.enable_test_cache is False
