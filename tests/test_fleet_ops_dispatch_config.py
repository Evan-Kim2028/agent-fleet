"""The ``fleet_ops.dispatch`` and ``fleet_ops.admission`` config blocks.

Both are optional and defaulted, so the thing worth testing is the contract: a
repo with no such keys behaves exactly as before, and a repo that sets them
gets the numbers it wrote — including when it writes nonsense.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from agent_fleet.fleet_ops.config import (
    DEFAULT_MAX_GATES,
    DEFAULT_MAX_LANES,
    AdmissionPoolConfig,
    DispatchConfig,
    FleetOpsConfig,
    load_fleet_ops_config,
    load_fleet_ops_config_from_repo,
)


def _load(section: dict[str, Any]) -> FleetOpsConfig:
    config = load_fleet_ops_config({"fleet_ops": section})
    assert config is not None
    return config


# ------------------------------------------------------------------- defaults


def test_absent_blocks_use_the_documented_defaults() -> None:
    config = _load({"base_branch": "main"})
    assert config.dispatch == DispatchConfig()
    assert config.admission == AdmissionPoolConfig()
    assert config.dispatch.max_lanes == DEFAULT_MAX_LANES
    assert config.dispatch.max_gates == DEFAULT_MAX_GATES
    assert config.admission.tests == 12
    assert config.admission.typecheck == 4


def test_the_gate_cap_default_is_deliberately_low() -> None:
    """18 gates at once is what drove the box to load 200."""
    assert DEFAULT_MAX_GATES <= 5


def test_a_fleet_ops_config_without_dispatch_defaults() -> None:
    assert FleetOpsConfig().dispatch.max_lanes == DEFAULT_MAX_LANES


# ---------------------------------------------------------------- dispatch


def test_dispatch_block_is_parsed() -> None:
    config = _load(
        {
            "dispatch": {
                "max_lanes": 3,
                "max_gates": 1,
                "psi_avg10_max": 12.5,
                "psi_path": "/custom/cpu.pressure",
                "cluster_order": ["C0", "C1"],
            }
        }
    )
    assert config.dispatch.max_lanes == 3
    assert config.dispatch.max_gates == 1
    assert config.dispatch.psi_avg10_max == pytest.approx(12.5)
    assert config.dispatch.psi_path == "/custom/cpu.pressure"
    assert config.dispatch.cluster_order == ("C0", "C1")


def test_a_scalar_cluster_order_is_accepted() -> None:
    assert _load({"dispatch": {"cluster_order": "C0"}}).dispatch.cluster_order == ("C0",)


@pytest.mark.parametrize("value", [0, -1, "many", None, [], {}])
def test_a_nonsense_max_lanes_falls_back_to_the_default(value: Any) -> None:  # noqa: ANN401
    assert _load({"dispatch": {"max_lanes": value}}).dispatch.max_lanes == DEFAULT_MAX_LANES


def test_a_nonsense_psi_max_falls_back() -> None:
    assert _load({"dispatch": {"psi_avg10_max": "high"}}).dispatch.psi_avg10_max == (
        DispatchConfig().psi_avg10_max
    )


def test_a_malformed_dispatch_block_falls_back() -> None:
    for value in ("nope", 5, [1, 2]):
        assert _load({"dispatch": value}).dispatch == DispatchConfig()


# --------------------------------------------------------------- admission


def test_admission_block_is_parsed() -> None:
    config = _load({"admission": {"tests": 4, "typecheck": 2, "shared_dir": "/srv/slots"}})
    assert config.admission.tests == 4
    assert config.admission.typecheck == 2
    assert config.admission.shared_dir == "/srv/slots"


def test_an_empty_admission_block_keeps_the_defaults() -> None:
    assert _load({"admission": {}}).admission == AdmissionPoolConfig()


@pytest.mark.parametrize("value", [0, -3, "lots", None])
def test_a_nonsense_pool_size_falls_back(value: Any) -> None:  # noqa: ANN401
    admission = _load({"admission": {"tests": value}}).admission
    assert admission.tests == 12


def test_a_malformed_admission_block_falls_back() -> None:
    assert _load({"admission": "no"}).admission == AdmissionPoolConfig()


def test_nice_zero_is_honoured_because_it_is_a_real_choice() -> None:
    assert _load({"admission": {"nice": 0}}).admission.nice == 0


# ---------------------------------------------------------------- from repo


def test_blocks_are_read_from_the_repo_yaml(tmp_path: Path) -> None:
    (tmp_path / ".agent-fleet.yaml").write_text(
        "fleet_ops:\n  dispatch:\n    max_lanes: 2\n    max_gates: 1\n  admission:\n    tests: 6\n",
        encoding="utf-8",
    )
    config = load_fleet_ops_config_from_repo(tmp_path)
    assert config is not None
    assert config.dispatch.max_lanes == 2
    assert config.dispatch.max_gates == 1
    assert config.admission.tests == 6


def test_a_repo_without_the_section_is_unaffected(tmp_path: Path) -> None:
    (tmp_path / ".agent-fleet.yaml").write_text("name: something\n", encoding="utf-8")
    assert load_fleet_ops_config_from_repo(tmp_path) is None


def test_the_new_keys_do_not_disturb_the_operator_block() -> None:
    config = _load(
        {
            "dispatch": {"max_lanes": 2},
            "operators": {"documents-0e": {"engine": "cmd", "push_branch": "fb/{lane}"}},
        }
    )
    spec = config.operator("documents-0e")
    assert spec is not None
    assert spec.engine == "cmd"
    assert spec.branch_for("alpha") == "fb/alpha"
