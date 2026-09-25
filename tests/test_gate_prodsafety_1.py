"""A judge-disabled gate config must not be rejected by the model policy.

Claim under test (``prodsafety-1``): ``_build_role_backends`` policy-checks all
four roles unconditionally, including ``ROLE_JUDGE``, so a config that sets
``gate.enable_judge: false`` and omits ``judge_model`` now raises
``ModelPolicyError`` in ``run_gate``'s preflight — before the pipeline runs —
turning the documented judge opt-out into a hard failure.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from agent_fleet.gate import pipeline as gate_pipeline
from agent_fleet.gate.config import GateConfig, load_gate_config
from agent_fleet.gate.pipeline import run_gate
from agent_fleet.model_policy import ModelPolicyError, parse_model_policy

SPACE_BUNNY = "stealth/space-bunny-alpha"

_JUDGE_DISABLED_RAW: dict[str, Any] = {
    "model_policy": {"backends": {"cmd": {"allowed_models": [SPACE_BUNNY]}}},
    "gate": {
        "backend": "cmd",
        "model": SPACE_BUNNY,
        "enable_judge": False,
        "enable_fix": False,
        "lenses": ["correctness"],
        "base_branch": "main",
        "agent_timeout_s": 5,
    },
}


@dataclass
class _FakeResult:
    stdout: str
    stderr: str = ""
    exit_code: int = 0
    duration_s: float = 0.0
    agent_id: str | None = None
    usage: dict[str, int] | None = None


@dataclass
class _FakeBackend:
    """Replays a scripted answer; records what it was asked for."""

    name: str = "fake"
    answers: dict[str, str] = field(default_factory=dict)
    default: str = ""
    prompts: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)

    def run(self, prompt: str, **kwargs: Any) -> _FakeResult:  # noqa: ANN401
        self.prompts.append(prompt)
        self.models.append(str(kwargs.get("model", "")))
        for needle, answer in self.answers.items():
            if needle in prompt:
                return _FakeResult(answer)
        return _FakeResult(self.default)


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return done.stdout


@pytest.fixture
def judge_disabled_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """A git repo plus a fleet.yaml that disables the judge and names no judge_model."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(tmp_path, "init", "-b", "main", str(repo))
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "src").mkdir()
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    (repo / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "change")
    _git(repo, "checkout", "-q", "-b", "feat")

    config = tmp_path / "fleet.yaml"
    config.write_text(yaml.safe_dump(_JUDGE_DISABLED_RAW), encoding="utf-8")

    # Keep every side effect of a real run inside tmp_path: slot pools and the
    # gate's home directory, so no test touches the machine-wide state.
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    return repo, config


def test_judge_disabled_config_with_no_judge_model_passes_the_preflight(
    judge_disabled_env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``gate.enable_judge: false`` with no ``judge_model`` must not fail the run.

    The judge is switched off, so its unmapped target (``judge_model`` defaults
    to ``None``) is never dispatched and must not be policy-checked. With the
    backend faked, the whole pipeline runs and returns a ``GateResult``.
    """
    repo, config_path = judge_disabled_env

    backend = _FakeBackend(
        name="cmd",
        default=json.dumps({"findings": []}),
    )
    monkeypatch.setattr(gate_pipeline, "build_gate_backend", lambda name: backend)

    result = run_gate(
        repo_path=repo,
        pr_number=7,
        config_path=str(config_path),
        gate_dir=Path(judge_disabled_env[0]).parent / "gate",
        use_systemd=False,
    )

    # The run completed normally instead of dying in the preflight.
    assert result is not None
    # The judge was genuinely disabled: no judge call was ever dispatched.
    assert not any("final pre-merge judge" in prompt for prompt in backend.prompts)


def test_disabled_judge_role_is_not_policy_checked(
    judge_disabled_env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preflight must skip ``ROLE_JUDGE`` when the judge is disabled.

    A backend that would itself fail the policy stands in for the judge slot: it
    proves the check is skipped for the disabled role rather than passing by
    accident, because every *enabled* role still has to satisfy the policy.
    """
    repo, config_path = judge_disabled_env
    raw = gate_pipeline._load_raw_config(str(config_path))
    policy = parse_model_policy(raw)
    gate_cfg = load_gate_config(raw) or GateConfig()
    assert gate_cfg.enable_judge is False
    # The disabled role resolves to an unmapped, model-less target.
    assert gate_cfg.role_target("judge").model is None

    built: list[str] = []
    monkeypatch.setattr(
        gate_pipeline,
        "build_gate_backend",
        lambda name: built.append(name) or _FakeBackend(name=name),
    )

    try:
        role_backends = gate_pipeline._build_role_backends(gate_cfg, policy)
    except ModelPolicyError as exc:  # pragma: no cover - the defect
        pytest.fail(f"disabled judge must not be policy-checked, but: {exc}")

    # No backend is routed for a role that will never dispatch, and the roles
    # that do run are present.
    assert "judge" not in role_backends
    assert {"find", "verify", "fix"} <= set(role_backends)
