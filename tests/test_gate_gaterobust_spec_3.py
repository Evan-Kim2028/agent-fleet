"""`gate --pr N` must escalate, not crash, when the PR cannot be resolved (spec-3).

``run_gate`` now derives the lane slug *before* constructing the pipeline, and
does so by calling ``resolve_pull_request(repo, pr_number)``. That call raises
:class:`GateError` whenever the PR cannot be resolved — ``gh`` not installed, not
authenticated, no remote, a PR that does not exist.

The pipeline's own ``run()`` wraps its work in ``try/except (GateInfraError,
GateError, ModelPolicyError, OSError)`` and turns any of those into a
NEEDS-ESCALATION result. This pre-run call is outside that block, and
``cmd_gate`` catches only ``ModelPolicyError``. So the operator's automerge
wrapper gets an unhandled traceback and a crash exit code instead of the
NEEDS-ESCALATION line and exit 1 that every other failure in this command
produces.

The escaping call is new on this branch; origin/main's ``run_gate`` made no such
pre-run lookup.
"""

from __future__ import annotations

import contextlib
import io
import json
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet.cli import main
from agent_fleet.contracts.gate import GateOutcome
from agent_fleet.gate.gitops import GateError

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def unresolvable_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    """No gh CLI / no auth / no such PR: exactly what the operator would hit.

    ``resolve_pull_request`` is the real entry point for every PR lookup the
    command makes, so this covers both the pipeline's own call and the
    pre-pipeline slug derivation. Everything downstream is left intact.
    """
    import agent_fleet.gate.pipeline as pipeline

    def _boom(_repo: Any, _pr: int) -> Any:  # noqa: ANN401
        raise GateError("gh CLI not found; cannot resolve the PR head")

    monkeypatch.setattr(pipeline, "resolve_pull_request", _boom)


@pytest.fixture
def fleet_config(tmp_path: Path) -> Path:
    """A minimal fleet.yaml: the gate defaults to a null model, which the model
    policy rejects before any PR resolution is attempted."""
    config = tmp_path / "fleet.yaml"
    config.write_text(
        "model_policy: {}\ngate:\n  backend: cmd\n  model: m\n  judge_backend: cmd\n"
        "  judge_model: m\n",
        encoding="utf-8",
    )
    return config


def test_gate_reports_an_unresolvable_pr_as_a_needs_escalation(
    tmp_path: Path,
    fleet_config: Path,
) -> None:
    """The command must print a status line and return an exit code, not raise."""
    exit_code: int | None = None
    traceback_text = ""
    try:
        with (
            contextlib.redirect_stderr(io.StringIO()) as err,
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            exit_code = main(
                [
                    "--config",
                    str(fleet_config),
                    "gate",
                    "--repo-path",
                    str(tmp_path / "repo"),
                    "--pr",
                    "1",
                ]
            )
            traceback_text = err.getvalue()
            stdout = out.getvalue()
    except GateError as exc:  # the defect: nothing in cmd_gate handles this
        pytest.fail(
            f"cmd_gate let a GateError escape as an unhandled crash: {exc!r}. "
            f"A gate run that cannot resolve its PR is a NEEDS-ESCALATION, not a "
            f"traceback -- see pipeline.run()'s handler for the same class of error"
        )

    assert exit_code == 1, f"expected a clean exit 1, got {exit_code}"
    assert "NEEDS-ESCALATION" in traceback_text, (
        f"no status line was written to stderr: {traceback_text!r}"
    )
    assert "Traceback" not in traceback_text, f"a traceback leaked: {traceback_text!r}"
    payload = json.loads(stdout)
    assert payload["outcome"] == GateOutcome.NEEDS_ESCALATION.value, payload
