"""The verifier stage must run on the verifier budget, not the lens budget.

``GateConfig.stage_timeout`` builds the field name as ``f"{role}_timeout_s"``.
The pipeline's verifier call site passes ``ROLE_VERIFIER``, which is
``"verifier"`` -- so it looks for ``verifier_timeout_s``, a field that does not
exist, and falls through the ``isinstance(value, int)`` check to the lens
budget. The configured ``gate.verify_timeout_s`` (documented in docs/GATE.md
and set in examples/fleet.gate.yaml) is parsed and stored but never applied.
"""

from agent_fleet.gate.config import GateConfig, load_gate_config
from agent_fleet.gate.pipeline import ROLE_FIX, ROLE_JUDGE, ROLE_LENS, ROLE_VERIFIER


def test_pipeline_role_names_match_the_config_field_names() -> None:
    """Every role the pipeline actually passes resolves to its own budget."""
    assert ROLE_VERIFIER == "verifier"
    cfg = GateConfig(
        lens_timeout_s=1111,
        verify_timeout_s=9999,
        judge_timeout_s=2222,
        fix_timeout_s=3333,
    )
    assert cfg.stage_timeout(ROLE_LENS) == 1111
    # The real call site: pipeline.py passes ROLE_VERIFIER, not "verify".
    assert cfg.stage_timeout(ROLE_VERIFIER) == 9999
    assert cfg.stage_timeout(ROLE_JUDGE) == 2222
    assert cfg.stage_timeout(ROLE_FIX) == 3333


def test_verify_timeout_s_from_fleet_yaml_reaches_the_verifier_stage() -> None:
    """An operator raising the verifier budget in fleet.yaml must take effect."""
    cfg = load_gate_config({"gate": {"lens_timeout_s": 1111, "verify_timeout_s": 9999}})
    assert cfg.verify_timeout_s == 9999
    assert cfg.stage_timeout(ROLE_VERIFIER) == 9999
