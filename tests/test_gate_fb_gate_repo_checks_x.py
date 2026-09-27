"""A red REQUIRED CHECK must block, not be recorded and then ignored.

The gate records a failing required check as a confirmed blocker, the same shape
a red test produces, and hands it to the fixer. But the full-evidence path only
ever consulted the *pytest* result when deciding it had converged: a green suite
with an open required-check blocker returned ``converged``, and ``converged`` is
the one metric outcome that maps to ``APPROVED``. So a PR that broke the repo's
second build — the entire reason ``gate.required_checks`` exists — merged with a
``PREMERGE-APPROVED`` line and no reason recorded, byte-identical to a PR whose
checks passed.

The test drives the real entry point against a real temp git repo, because every
seam that matters here is real: the check is selected from an actual ``git diff``,
``db/schema.sql`` is what routes the PR to the full evidence gate rather than the
STANDARD bar, and the approval is written by ``run()``'s own outcome mapping. Only
the two expensive fakes are stubbed — pytest (green) and the model (clean, with a
fixer that pushes nothing).

The control is the assertion that matters most: the *same* pipeline with a check
that exits 0 approves. Without it, "not approved" could be satisfied by a fixture
that never selected the check at all, which is the other way this feature can
quietly do nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 - built into concrete paths at runtime
from typing import Any

import pytest

from agent_fleet.gate.config import load_gate_config
from agent_fleet.gate.pipeline import (
    GatePipeline,
    GateTestRunner,
    PullRequestRef,
    TestRun,
)
from agent_fleet.model_policy import parse_model_policy

# ---------------------------------------------------------------------------
# Fixtures: a real git repo, and a clean model
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo whose PR touches a dbt model (selects the check) and a schema file.

    Both files are load-bearing. The model is what ``when_paths: ['^transform/']``
    matches, and ``db/schema.sql`` is what ``sensitive_paths`` matches, which is
    what keeps the PR off the STANDARD bar and onto the convergence path where
    the blocker used to be dropped. A repro that left the full gate would prove
    nothing about the bug, which only exists on that path.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git(tmp_path, "init", "-q", "-b", "main", str(root))
    _git(root, "config", "user.email", "gate@test.local")
    _git(root, "config", "user.name", "Gate Test")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")

    _git(root, "checkout", "-q", "-b", "pr")
    model = root / "transform" / "models" / "stg_orders"
    model.mkdir(parents=True)
    (model / "stg_orders.sql").write_text("select 1\n", encoding="utf-8")
    (root / "db").mkdir()
    (root / "db" / "schema.sql").write_text("create table t(i int);\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "pr change")
    return root


@dataclass
class _CleanBackend:
    """Every reviewer reports a clean PR, and the fixer changes nothing.

    A reviewer that found a blocker would mask the defect, and a fixer that pushed
    a fix would let the run legitimately converge later. Both are excluded on
    purpose: this is the shape of the PR that slipped through — everyone said it
    was fine except the command that actually builds the repo.
    """

    prompts: list[str] = field(default_factory=list)
    fixer_runs: int = 0

    def run(self, prompt: str, **_kwargs: Any) -> Any:  # noqa: ANN401
        self.prompts.append(prompt)
        if _is_fixer(prompt):
            # The fixer is told to commit and push. It does nothing, which the
            # pipeline reads as "no push" — a refusal, not a pass.
            self.fixer_runs += 1
            return _Result("")
        return _Result(_clean_answer(prompt))


@dataclass(frozen=True)
class _Result:
    stdout: str
    stderr: str = ""
    exit_code: int = 0
    duration_s: float = 0.0
    agent_id: str | None = None
    usage: dict[str, int] | None = None


def _is_fixer(prompt: str) -> bool:
    """Whether *prompt* is the fixer's, identified by its own instruction line.

    Every role runs in agent mode against the same worktree, so mode cannot
    separate them. The fixer is the only role told to push, and the sentence that
    says so is generated in one place, so matching it is a stable seam rather
    than a guess at wording.
    """
    return "These tests FAIL right now" in prompt


def _clean_answer(prompt: str) -> str:
    """An empty answer in whichever shape the schema shown in *prompt* requires.

    The gate validates every reviewer answer against a closed JSON schema
    (``additionalProperties: false``), so one canned object cannot answer both a
    lens and a judge. Each role's own spec is printed in its prompt, so the shape
    is read off the prompt instead of guessed from the call order.
    """
    if '"untestable_rulings"' in prompt:
        return json.dumps({"untestable_rulings": [], "new_blockers": []})
    if '"verdict"' in prompt:
        return json.dumps({"verdict": "REJECTED", "reason": "not reproduced", "test_file": None})
    if '"unresolved"' in prompt:
        return json.dumps({"unresolved": []})
    return json.dumps({"findings": []})


def _red_check() -> str:
    """A check that always fails, as a real command the gate can execute."""
    return f'{sys.executable} -c "import sys; sys.exit(1)"'


def _green_check() -> str:
    return f'{sys.executable} -c "import sys; sys.exit(0)"'


def _build_pipeline(
    repo: Path, tmp_path: Path, *, command: str
) -> tuple[GatePipeline, _CleanBackend]:
    config = load_gate_config(
        {
            "gate": {
                "backend": "cmd",
                "model": "m",
                "judge_backend": "cmd",
                "judge_model": "m",
                "enable_fix": True,
                "required_checks": [
                    {"name": "dbt-compile", "command": command, "when_paths": [r"^transform/"]}
                ],
            }
        }
    )
    assert config is not None, "gate must not be disabled by this config"
    backend = _CleanBackend()
    pipeline = GatePipeline(
        repo=repo,
        pr_number=1,
        config=config,
        policy=parse_model_policy({}),
        backend=backend,  # type: ignore[arg-type]
        judge_backend=backend,  # type: ignore[arg-type]
        gate_dir=tmp_path / "gate",
        use_systemd=False,
    )
    return pipeline, backend


def _wire_git_for_run(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """Stub only what needs a forge; keep the worktree and diff calls real.

    ``resolve_pull_request`` and ``current_pr_head`` shell out to ``gh``, which
    does not exist for a local fixture repo. Everything they answer *about* —
    worktree creation, the changed-path list that selects the check and the tier
    — stays real, because those are the parts whose correctness this test is
    about.
    """
    import agent_fleet.gate.pipeline as pipeline_mod

    head = _git(repo, "rev-parse", "pr")
    monkeypatch.setattr(pipeline_mod, "fetch_base", lambda *_a, **_k: None)
    monkeypatch.setattr(
        pipeline_mod,
        "resolve_pull_request",
        lambda *_a, **_k: PullRequestRef(
            number=1, head_ref="pr", head_sha=head, state="OPEN", base_ref="main"
        ),
    )
    monkeypatch.setattr(pipeline_mod, "current_pr_head", lambda *_a, **_k: head)
    # The gate re-runs its own gate tests plus the PR's; a green run is the
    # precondition of the bug, so it is stubbed to the fact it stands for.
    monkeypatch.setattr(GateTestRunner, "run", lambda _self, _files: TestRun())
    # Metrics land in the operator's real ~/.agent-fleet; a test must not append.
    monkeypatch.setattr(
        "agent_fleet.gate.metrics.GateMetrics.append_metrics", lambda _self, *_a, **_k: None
    )
    monkeypatch.setattr("agent_fleet.gate.metrics.read_metrics", lambda *_a, **_k: [])


# ---------------------------------------------------------------------------
# The defect
# ---------------------------------------------------------------------------


def test_a_red_required_check_blocks_a_pr_with_a_green_suite(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check that failed must not be approved on the strength of pytest alone.

    This is the whole feature in one assertion. ``dbt-compile`` exits non-zero,
    which the gate records as a confirmed blocker, and pytest is green. The
    approval has to go to the check: the gate exists because that repo has a
    second build, and a green unit suite says nothing about it.
    """
    _wire_git_for_run(monkeypatch, repo)
    pipeline, _backend = _build_pipeline(repo, tmp_path, command=_red_check())

    result = pipeline.run()

    # Precondition: the check really ran and really failed, so a non-approval
    # below is the check blocking rather than the fixture failing to select it.
    check_rows = [c for c in result.confirmed if c.get("source") == "required-check"]
    assert len(check_rows) == 1, f"the red check was not recorded: {result.confirmed}"
    assert result.metrics is not None
    # The metrics row carries one entry per head the check was measured on (head,
    # then each convergence head), and every one of them is this red check.
    assert result.metrics.checks
    assert {c["name"] for c in result.metrics.checks} == {"dbt-compile"}
    assert not any(c["passed"] for c in result.metrics.checks)

    assert not result.approved, (
        f"a red required check approved the PR: {result.status_line} {result.reasons}"
    )
    assert "PREMERGE-APPROVED" not in result.status_line


def test_a_green_required_check_still_approves(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: the identical pipeline approves when the check passes.

    Without this, a fix that simply refused everything would satisfy the test
    above. Approval must depend on the check's exit code, not on the gate having
    lost the ability to approve.
    """
    _wire_git_for_run(monkeypatch, repo)
    pipeline, _backend = _build_pipeline(repo, tmp_path, command=_green_check())

    result = pipeline.run()

    assert result.approved, f"a green check blocked a clean PR: {result.reasons}"
    assert result.metrics is not None
    assert result.metrics.checks
    assert {c["name"] for c in result.metrics.checks} == {"dbt-compile"}
    assert all(c["passed"] for c in result.metrics.checks)


def test_a_red_required_check_is_named_in_the_reasons(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal must say which check failed.

    An escalation whose reason line is empty is the second half of the same bug:
    the run refuses, but the operator is handed nothing to act on, so the red
    check is still invisible after the fact.
    """
    _wire_git_for_run(monkeypatch, repo)
    pipeline, _backend = _build_pipeline(repo, tmp_path, command=_red_check())

    result = pipeline.run()

    assert not result.approved
    joined = " ".join(result.reasons)
    assert "dbt-compile" in joined, f"the refusal does not name the check: {result.reasons}"
