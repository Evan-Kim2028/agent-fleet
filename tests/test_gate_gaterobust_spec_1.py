"""The VERIFY stage must actually receive the operator's ``verify_timeout_s``.

``stage_timeout(role)`` derives the config field as ``f"{role}_timeout_s"``.
The pipeline passes its own vocabulary for every stage, and the verifier
constant is ``"verifier"`` — so the field it looks up is
``verifier_timeout_s``, which does not exist. The lookup misses, the helper
falls back to ``lens_timeout_s``, and ``gate.verify_timeout_s`` (a documented,
published key, default 2400) is dead config: an operator who budgets the
verifier 10 minutes is still handing it the reviewing budget.

The existing unit test uses the literal ``"verify"`` rather than the constant
the pipeline passes, which is exactly why the suite stays green over this.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_fleet.contracts.gate import Finding
from agent_fleet.gate.config import GateConfig, load_gate_config
from agent_fleet.gate.pipeline import ROLE_VERIFIER, GatePipeline
from agent_fleet.model_policy import ModelPolicy

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_VERIFY_ANSWER = (
    "```json\n"
    '{"verdict": "CONFIRMED", "test_file": "tests/test_gate_lane_spec-1.py",'
    ' "reason": "role vocabulary mismatch"}\n'
    "```"
)


class _Result:
    def __init__(self, exit_code: int, stdout: str = "") -> None:
        self.stdout = stdout
        self.stderr = ""
        self.exit_code = exit_code
        self.duration_s = 1.0


class _RecordingBackend:
    """Captures the timeout the verifier call was actually given."""

    def __init__(self) -> None:
        self.timeouts: list[int] = []

    def run(self, _prompt: str, **kwargs: Any) -> _Result:  # noqa: ANN401
        self.timeouts.append(int(kwargs["timeout_s"]))
        return _Result(0, _VERIFY_ANSWER)


def _pipeline(tmp_path: Path, backend: object) -> GatePipeline:
    config = GateConfig(
        backend="cmd",
        model="m",
        judge_backend="cmd",
        judge_model="m",
        enable_judge=False,
        enable_fix=False,
        lens_timeout_s=2400,
        verify_timeout_s=600,
    )
    return GatePipeline(
        repo=tmp_path / "repo",
        pr_number=7,
        config=config,
        policy=ModelPolicy(backends={}),
        backend=backend,  # type: ignore[arg-type]
        gate_dir=tmp_path / "gate",
        use_systemd=False,
        lane_slug="fb/lane",
    )


def test_the_role_name_the_pipeline_passes_resolves_to_verify_timeout_s() -> None:
    """The mapping is role-name based, so the pipeline's own constant must work."""
    cfg = GateConfig(lens_timeout_s=2400, verify_timeout_s=600)
    assert cfg.stage_timeout(ROLE_VERIFIER) == 600


def test_the_verify_timeout_s_key_is_not_dead_config() -> None:
    cfg = load_gate_config({"gate": {"lens_timeout_s": 2400, "verify_timeout_s": 600}})
    assert cfg is not None
    assert cfg.stage_timeout(ROLE_VERIFIER) == 600, (
        "gate.verify_timeout_s is ignored: the pipeline asks for the 'verifier' "
        "role, which derives 'verifier_timeout_s', so the verifier inherits "
        "lens_timeout_s and the published key configures nothing"
    )


def test_the_verifier_call_is_given_the_configured_verify_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: drive ``GatePipeline.verify`` and read the budget it hands the agent."""
    monkeypatch.setattr("agent_fleet.gate.pipeline.resolve_diff_base", lambda *_a: "origin/main")
    monkeypatch.setattr(GatePipeline, "_head_sha", lambda *_a: "a" * 40)

    backend = _RecordingBackend()
    pipe = _pipeline(tmp_path, backend)
    pipe.verify(
        tmp_path / "wt",
        [
            Finding(
                id="spec-1",
                file="agent_fleet/gate/config.py",
                line=131,
                claim="stage_timeout derives the wrong field name for the verifier",
                repro="stage_timeout(ROLE_VERIFIER) returns lens_timeout_s",
                lens="correctness",
            )
        ],
        source="lens",
    )

    assert backend.timeouts == [600], (
        f"the verifier agent was run with a {backend.timeouts} budget, not the "
        "configured verify_timeout_s=600"
    )
