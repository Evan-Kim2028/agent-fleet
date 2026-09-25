"""A role the config disables must not be validated by the pre-flight check.

``run_gate`` resolves every role through ``GateConfig.role_target`` and checks all
four against the model policy before anything is constructed. A role that is
disabled (``enable_judge: false``, ``enable_fix: false``) is never dispatched, so
its target need not be dispatchable: a config that has always worked — a global
``model`` for the find/verify/fix lane, and a ``judge_backend``/``judge_model``
pair left at its defaults because the judge is off — must keep working.

Both configs below are ones that pass ``run_gate``'s pre-flight on ``origin/main``,
where the judge check sat inside ``if gate_cfg.enable_judge`` and only the lens
role was checked. Each must still get past pre-flight here, and reach the run.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_fleet.gate import pipeline as gate_pipeline
from agent_fleet.gate.pipeline import run_gate
from agent_fleet.model_policy import ModelPolicyError

SPACE_BUNNY = "stealth/space-bunny-alpha"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return done.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(tmp_path, "init", "-b", "main", str(path))
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    (path / "src.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "base")
    (path / "src.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "change")
    return path


def _run_pre_flight(
    tmp_path: Path, repo: Path, raw: dict[str, Any]
) -> Any:  # noqa: ANN401 - GateResult
    """Drive ``run_gate`` as far as the pre-flight, with nothing past it real.

    Backends and the run itself are stubbed so the only thing this exercises is
    the policy check ``run_gate`` performs before dispatching anything.
    """
    import yaml

    config_path = tmp_path / "fleet.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    sentinel = object()
    gate_dir = tmp_path / "gate"

    def _fake_build(backend_name: str) -> Any:  # noqa: ANN401
        class _Backend:
            name = backend_name

            def run(self, prompt: str, **kwargs: Any) -> Any:  # noqa: ANN401
                raise AssertionError("no backend call was expected in this test")

        return _Backend()

    monkey = pytest.MonkeyPatch()
    monkey.setattr(gate_pipeline, "build_gate_backend", _fake_build)
    monkey.setattr(gate_pipeline.GatePipeline, "run", lambda self: sentinel)
    monkey.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    try:
        return run_gate(
            repo_path=repo,
            pr_number=7,
            config_path=str(config_path),
            gate_dir=gate_dir,
            use_systemd=False,
        )
    finally:
        monkey.undo()


def test_judge_disabled_run_does_not_require_a_judge_model(
    tmp_path: Path, repo: Path
) -> None:
    """``enable_judge: false`` with no ``judge_model`` must not abort the run.

    ``role_target("judge")`` falls back to ``judge_backend``/``judge_model``, whose
    model defaults to ``None``; the judge is never dispatched, so that target does
    not need to satisfy the policy.
    """
    result = None
    try:
        result = _run_pre_flight(
            tmp_path,
            repo,
            {"gate": {"backend": "cmd", "model": SPACE_BUNNY, "enable_judge": False}},
        )
    except ModelPolicyError as exc:
        pytest.fail(
            "a judge-disabled run was aborted by the pre-flight policy check on a "
            f"role it never dispatches: {exc}"
        )

    assert result is not None, "run_gate returned nothing for a judge-disabled run"


def test_judge_disabled_run_is_not_blocked_by_a_policy_excluding_judge(
    tmp_path: Path, repo: Path
) -> None:
    """A policy pinning ``cmd`` away from ``judge`` must not stop a judge-free run.

    ``cmd`` is allowed ``[lens, verifier, fix]`` — the roles this config actually
    dispatches. Checking the disabled judge role anyway raises
    "may not serve role 'judge'" and aborts a run that has nothing to do with the
    judge.
    """
    result = None
    try:
        result = _run_pre_flight(
            tmp_path,
            repo,
            {
                "model_policy": {
                    "backends": {
                        "cmd": {
                            "allowed_models": [SPACE_BUNNY],
                            "roles": ["lens", "verifier", "fix"],
                        }
                    }
                },
                "gate": {"backend": "cmd", "model": SPACE_BUNNY, "enable_judge": False},
            },
        )
    except ModelPolicyError as exc:
        pytest.fail(
            "a judge-disabled run was aborted by the pre-flight policy check on a "
            f"role it never dispatches: {exc}"
        )

    assert result is not None, "run_gate returned nothing for a judge-disabled run"
