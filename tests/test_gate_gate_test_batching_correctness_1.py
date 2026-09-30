"""A verifier that answers with an absolute path loses its own failing test.

`_normalise_repo_path` strips `./` and backslashes but never strips an absolute
worktree prefix, so a verifier that names its test by absolute path -- which the
`test_file` schema permits, since it is only typed `string` -- produces a `rel`
that is an absolute path. Two things then go wrong, and both happen on a claim
that is genuinely true:

1. `failed_tests_in(rel)` builds the prefix `/abs/path/wt/tests/test_x.py::`,
   which never matches the repo-relative node id pytest actually reports
   (`tests/test_x.py::test_x`), so the claim's own failing test credits nothing
   and `_settle` takes the "own is empty" branch. That is a regression against
   main, which confirmed on the global `tests_failed` flag and so never needed
   the answer to be repo-relative.
2. `_unlink` then DELETES the verifier's test file -- the only artifact of the
   finding -- so a real blocker is discarded, its evidence destroyed, and
   `evidence.gate_tests` stays empty, leaving nothing for judge or converge.

A path is trivially convertible to repo-relative, so the claim must survive.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agent_fleet.contracts.gate import Finding
from agent_fleet.gate.config import GateConfig
from agent_fleet.gate.pipeline import GatePipeline, GateTestRunner, TestRun
from agent_fleet.model_policy import ModelPolicy

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


@dataclass
class _Backend:
    """Answers a verify call with the JSON the finding id asked for."""

    verdicts: dict[str, str] = field(default_factory=dict)

    def run(self, prompt: str, **_kwargs: Any) -> Any:  # noqa: ANN401
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


def _finding(fid: str = "g-1") -> Finding:
    return Finding(
        id=fid,
        lens="correctness",
        file="agent_fleet/gate/pipeline.py",
        line=1159,
        claim="a real defect",
        repro="input -> wrong output",
        testable=True,
    )


def _pipeline(tmp_path: Path, backend: _Backend) -> GatePipeline:
    return GatePipeline(
        repo=tmp_path / "repo",
        pr_number=42,
        config=GateConfig(backend="cmd", model="m"),
        policy=ModelPolicy(backends={}),
        backend=backend,  # type: ignore[arg-type]
        judge_backend=None,
        gate_dir=tmp_path / "gate",
        use_systemd=False,
    )


def _worktree_with_failing_test(tmp_path: Path, rel: str) -> tuple[Path, Path]:
    wt = tmp_path / "wt"
    test_path = wt / rel
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text("def test_x():\n    assert False\n", encoding="utf-8")
    return wt, test_path


def test_absolute_test_file_answer_discards_a_truly_failing_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verifier's own test failed; the claim is real and must be confirmed."""
    rel = "tests/test_gate_good.py"
    wt, test_path = _worktree_with_failing_test(tmp_path, rel)
    absolute = str(test_path.resolve())

    backend = _Backend(
        verdicts={
            "g-1": json.dumps({"verdict": "CONFIRMED", "test_file": absolute, "reason": "boom"})
        }
    )
    pipe = _pipeline(tmp_path, backend)

    # What the batched re-run really reports: the repo-relative node id of the
    # verifier's own failing test, and no infra error.
    def run(_self: GateTestRunner, _files: list[str]) -> TestRun:
        return TestRun(failing=[f"{rel}::test_x"], ran=1, tests_failed=True)

    monkeypatch.setattr(GateTestRunner, "run", run)

    pipe.verify(wt, [_finding()], source="lens")

    assert [c["id"] for c in pipe.evidence.confirmed] == ["g-1"], (
        "a verifier naming its test by absolute path had its genuinely failing "
        f"test attributed to nothing: rejected={pipe.evidence.rejected_items}"
    )
    assert pipe.evidence.gate_tests, (
        "the confirmed blocker recorded no gate test, so judge and converge "
        "cannot see the evidence at all"
    )
    assert test_path.exists(), (
        "the verifier's only evidence was deleted, because the absolute-path "
        "answer never matched a repo-relative failing node id"
    )
