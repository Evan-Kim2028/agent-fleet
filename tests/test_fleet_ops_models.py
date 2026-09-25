"""Model policy: cmd is pinned, grok may only judge."""

from __future__ import annotations

import pytest

from agent_fleet.fleet_ops.models import (
    CMD_MODEL,
    DEVIN_MODEL_LADDER,
    GROK_JUDGE_MODEL,
    ModelPolicyError,
    enforce_implementation_model,
    resolve_engine_model,
)


def test_cmd_engine_resolves_to_space_bunny() -> None:
    assert resolve_engine_model("cmd") == CMD_MODEL
    assert resolve_engine_model("CMD") == CMD_MODEL


def test_grok_is_only_permitted_as_judge() -> None:
    assert resolve_engine_model("grok", role="judge") == GROK_JUDGE_MODEL


def test_grok_rejected_for_implementation() -> None:
    with pytest.raises(ModelPolicyError, match="judge"):
        resolve_engine_model("grok", role="implement")


def test_devin_uses_the_sanctioned_ladder() -> None:
    assert resolve_engine_model("devin") == DEVIN_MODEL_LADDER[0]
    # enforce pins to the resolved default; the ladder is validated by the
    # devin engine itself (a rung below the default is a legitimate fallback).
    assert enforce_implementation_model("devin", None) == DEVIN_MODEL_LADDER[0]


def test_enforce_accepts_the_policy_model() -> None:
    assert enforce_implementation_model("cmd", CMD_MODEL) == CMD_MODEL


def test_enforce_defaults_when_no_model_requested() -> None:
    assert enforce_implementation_model("cmd", None) == CMD_MODEL
    assert enforce_implementation_model("cmd", "  ") == CMD_MODEL


def test_enforce_rejects_a_foreign_model() -> None:
    """A stray FB_MODEL / AGENT_FLEET_MODEL must not redirect the lane."""
    with pytest.raises(ModelPolicyError, match="pinned"):
        enforce_implementation_model("cmd", "step-5-preview")


def test_enforce_rejects_a_foreign_devin_model() -> None:
    with pytest.raises(ModelPolicyError, match="pinned"):
        enforce_implementation_model("devin", "some-other-model")


def test_unknown_engine_is_rejected() -> None:
    with pytest.raises(ModelPolicyError, match="unknown engine"):
        resolve_engine_model("nope")


def test_unknown_role_is_rejected() -> None:
    with pytest.raises(ModelPolicyError, match="unknown model role"):
        resolve_engine_model("cmd", role="whatever")


def test_judge_role_rejected_for_cmd() -> None:
    """Only grok has a judge model; cmd judging is a config error, not a default."""
    with pytest.raises(ModelPolicyError, match="not valid"):
        resolve_engine_model("cmd", role="judge")
