"""A judge-disabled gate config must still be admitted by the model policy.

Claim: ``_build_role_backends`` policy-checks all four roles unconditionally, so
a config with ``enable_judge: false`` and no ``judge_model`` -- or a policy that
restricts ``cmd`` to the non-judge roles -- now raises ``ModelPolicyError`` at
startup, although the same config ran on origin/main (there, the judge check was
guarded by ``enable_judge``).

Reproduces the reported repro:
``model_policy.backends.cmd = {allowed_models: [m], roles: [lens, verifier, fix]}``
with ``gate = {backend: cmd, model: m, enable_judge: false}``, and the
unrestricted-roles variant where the judge role resolves to ``model=None``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
import yaml

from agent_fleet import cli
from agent_fleet.gate import pipeline as pipeline_mod
from agent_fleet.gate.config import GateConfig, load_gate_config
from agent_fleet.gate.pipeline import _build_role_backends
from agent_fleet.model_policy import ModelPolicyError, parse_model_policy

MODEL = "stealth/space-bunny-alpha"

BASES = [
    pytest.param(
        ["lens", "verifier", "fix"],
        id="policy-restricts-cmd-to-non-judge-roles",
    ),
    pytest.param(
        None,
        id="unrestricted-roles-judge-model-unset",
    ),
]


def _raw(roles: list[str] | None) -> dict[str, object]:
    policy: dict[str, object] = {"allowed_models": [MODEL]}
    if roles is not None:
        policy["roles"] = roles
    return {
        "model_policy": {"backends": {"cmd": policy}},
        "gate": {
            "enabled": True,
            "backend": "cmd",
            "model": MODEL,
            "enable_judge": False,
        },
    }


def _write(tmp_path: Path, raw: dict[str, object]) -> str:
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("roles", BASES)
def test_judge_disabled_config_passes_role_policy(roles: list[str] | None) -> None:
    """``enable_judge: false`` must exempt the judge role from the policy check."""
    raw = _raw(roles)
    gate_cfg = load_gate_config(raw)
    assert gate_cfg is not None
    assert gate_cfg.enable_judge is False

    policy = parse_model_policy(raw)

    try:
        built = _build_role_backends(gate_cfg, policy)
    except ModelPolicyError as exc:
        pytest.fail(
            "judge-disabled config rejected by the model policy "
            f"(roles={roles!r}): {exc}"
        )

    # The three live roles are routed and share the one built backend. The judge
    # is exempt and absent: it never dispatches, so no backend is routed for it.
    assert set(built) == {"find", "verify", "fix"}
    assert all(backend is built["find"] for backend in built.values())


@pytest.mark.parametrize("roles", BASES)
def test_cmd_gate_does_not_fail_fast_on_judge_policy_violation(
    roles: list[str] | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``agent-fleet gate`` must not exit 1 with a judge policy error here."""
    config_path = _write(tmp_path, _raw(roles))
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))

    class _Approved(Exception):
        """Stands in for a completed, approved gate run."""

    calls: list[dict[str, object]] = []

    def _fake_run_gate(**kwargs: object) -> object:
        # The real ``run_gate`` resolves the config and policy, then validates
        # every role's dispatch target before any backend is built. Reproduce
        # exactly that preflight so the test observes the real check; only the
        # agent work afterwards is stubbed out.
        raw = pipeline_mod._load_raw_config(kwargs.get("config_path"))  # noqa: SLF001
        policy = parse_model_policy(raw)
        gate_cfg = load_gate_config(raw) or GateConfig()
        pipeline_mod._build_role_backends(gate_cfg, policy)  # noqa: SLF001
        calls.append(kwargs)
        raise _Approved()

    monkeypatch.setattr("agent_fleet.gate.pipeline.run_gate", _fake_run_gate)

    args = argparse.Namespace(
        pr=123,
        repo_path=str(repo),
        task_file=None,
        status_file=None,
        config=config_path,
    )
    try:
        rc = cli.cmd_gate(args)
    except _Approved:
        rc = 0

    assert calls, "gate run never started: startup policy check rejected the config"
    assert rc == 0, f"gate exited {rc} before running"


def test_default_gate_config_model_is_settable() -> None:
    """Sanity: ``model`` round-trips through the config so the repro is real."""
    cfg = GateConfig(backend="cmd", model=MODEL, enable_judge=False)
    assert load_gate_config({"gate": {"model": MODEL, "enable_judge": False}}).model == MODEL
    assert json.loads(json.dumps(cfg.roles)) == cfg.roles
