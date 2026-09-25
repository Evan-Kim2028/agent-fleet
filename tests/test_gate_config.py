"""Tests for the gate configuration layer."""

from __future__ import annotations

import pytest

from agent_fleet.gate.config import (
    DEFAULT_LENS_ORDER,
    GateConfig,
    load_gate_config,
)


def test_absent_section_yields_defaults() -> None:
    cfg = load_gate_config({})
    assert cfg is not None
    assert cfg.lenses == DEFAULT_LENS_ORDER
    assert cfg.backend == "cmd"
    assert cfg.judge_backend == "cmd"
    assert cfg.max_fix_rounds == 4


def test_gate_false_disables_the_gate() -> None:
    assert load_gate_config({"gate": False}) is None


def test_none_config_yields_defaults() -> None:
    cfg = load_gate_config(None)
    assert cfg is not None
    assert cfg.lenses == DEFAULT_LENS_ORDER


def test_default_lens_focus_is_populated() -> None:
    cfg = load_gate_config({})
    assert cfg is not None
    for lens in cfg.lenses:
        assert cfg.focus_for(lens)


def test_lens_mapping_form_replaces_the_defaults() -> None:
    cfg = load_gate_config({"gate": {"lenses": {"perf": "Hot-path regressions only."}}})
    assert cfg is not None
    assert cfg.lenses == ("perf",)
    assert cfg.focus_for("perf") == "Hot-path regressions only."


def test_lens_list_form_keeps_default_focus_text() -> None:
    cfg = load_gate_config({"gate": {"lenses": ["correctness", "custom"]}})
    assert cfg is not None
    assert cfg.lenses == ("correctness", "custom")
    # A listed lens with no custom text falls back to its own name.
    assert cfg.focus_for("custom") == "custom"
    assert "Logic errors" in cfg.focus_for("correctness")


def test_lens_focus_merges_over_the_mapping_form() -> None:
    cfg = load_gate_config(
        {
            "gate": {
                "lenses": {"perf": "one"},
                "lens_focus": {"perf": "two", "other": "three"},
            }
        }
    )
    assert cfg is not None
    assert cfg.focus_for("perf") == "two"
    assert cfg.focus_for("other") == "three"
    assert "other" not in cfg.lenses


def test_scalar_overrides() -> None:
    cfg = load_gate_config(
        {
            "gate": {
                "max_candidates": 3,
                "max_fix_rounds": 7,
                "max_parallel_lenses": 2,
                "max_parallel_verifiers": 1,
                "agent_slots": 11,
                "test_slots": 2,
                "test_timeout_s": 60,
            }
        }
    )
    assert cfg is not None
    assert cfg.max_candidates == 3
    assert cfg.max_fix_rounds == 7
    assert cfg.max_parallel_lenses == 2
    assert cfg.max_parallel_verifiers == 1
    assert cfg.agent_slots == 11
    assert cfg.test_slots == 2
    assert cfg.test_timeout_s == 60


def test_string_and_optional_string_overrides() -> None:
    cfg = load_gate_config(
        {
            "gate": {
                "backend": "grok",
                "judge_backend": "cmd",
                "base_branch": "trunk",
                "push_branch": "fb/lane",
                "package_dir": "api",
                "test_memory": "4G",
            }
        }
    )
    assert cfg is not None
    assert cfg.backend == "grok"
    assert cfg.judge_backend == "cmd"
    assert cfg.base_branch == "trunk"
    assert cfg.push_branch == "fb/lane"
    assert cfg.package_dir == "api"
    assert cfg.test_memory == "4G"


def test_optional_strings_stay_none_when_absent() -> None:
    cfg = load_gate_config({"gate": {}})
    assert cfg is not None
    assert cfg.model is None
    assert cfg.judge_model is None
    assert cfg.push_branch is None
    assert cfg.package_dir is None


def test_bool_overrides() -> None:
    cfg = load_gate_config({"gate": {"enable_fix": False, "enable_judge": False}})
    assert cfg is not None
    assert cfg.enable_fix is False
    assert cfg.enable_judge is False


def test_non_dict_section_falls_back_to_defaults() -> None:
    cfg = load_gate_config({"gate": "enabled"})
    assert cfg is not None
    assert cfg.lenses == DEFAULT_LENS_ORDER


def test_empty_lens_mapping_keeps_defaults() -> None:
    cfg = load_gate_config({"gate": {"lenses": {}}})
    assert cfg is not None
    assert cfg.lenses == DEFAULT_LENS_ORDER


def test_default_config_is_frozen() -> None:
    """A frozen config means one gate run cannot mutate another's settings."""
    cfg = GateConfig()
    with pytest.raises(Exception, match=r"frozen|assign"):
        cfg.max_fix_rounds = 9  # type: ignore[misc]
