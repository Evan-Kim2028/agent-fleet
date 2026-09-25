"""Tests for agent_fleet.model_policy — the machine-wide model allowlist."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_fleet.model_policy import (
    BackendPolicy,
    ModelPolicy,
    ModelPolicyError,
    parse_model_policy,
)

POLICY_RAW = {
    "model_policy": {
        "backends": {
            "cmd": {"allowed_models": ["stealth/space-bunny-alpha"]},
            "grok": {"allowed_models": ["step-5-preview"], "roles": ["judge"]},
        }
    }
}


def test_approved_model_passes() -> None:
    policy = parse_model_policy(POLICY_RAW)
    assert policy.check(backend="cmd", model="stealth/space-bunny-alpha", role="lens") == (
        "stealth/space-bunny-alpha"
    )


def test_unapproved_model_fails_fast() -> None:
    policy = parse_model_policy(POLICY_RAW)
    with pytest.raises(ModelPolicyError) as exc:
        policy.check(backend="cmd", model="grok-4.6", role="lens")
    assert "stealth/space-bunny-alpha" in str(exc.value)


def test_role_restriction_blocks_grok_outside_judge() -> None:
    """grok is the judge backend only; a find/verify/fix call must be refused."""
    policy = parse_model_policy(POLICY_RAW)
    assert policy.check(backend="grok", model="step-5-preview", role="judge")
    for role in ("lens", "verifier", "fix"):
        with pytest.raises(ModelPolicyError, match="may not serve role"):
            policy.check(backend="grok", model="step-5-preview", role=role)


def test_missing_model_is_refused_for_a_pinned_backend() -> None:
    policy = parse_model_policy(POLICY_RAW)
    with pytest.raises(ModelPolicyError, match="requires an explicit model"):
        policy.check(backend="cmd", model=None, role="lens")


def test_unpinned_backend_requires_a_model_but_is_not_restricted() -> None:
    policy = parse_model_policy(POLICY_RAW)
    assert policy.check(backend="cursor", model="composer-2.5", role="lens") == "composer-2.5"
    with pytest.raises(ModelPolicyError, match="no model given"):
        policy.check(backend="cursor", model=None, role="lens")


def test_empty_policy_allows_anything_with_a_model() -> None:
    policy = parse_model_policy({})
    assert policy.backends == {}
    assert policy.check(backend="cmd", model="whatever", role="lens") == "whatever"


def test_parse_is_case_insensitive_on_backend_names() -> None:
    policy = parse_model_policy(POLICY_RAW)
    assert policy.check(backend="CMD", model="stealth/space-bunny-alpha", role="lens")


def test_malformed_policy_sections_are_ignored() -> None:
    """A typo in the policy must not silently pin nothing — it must not raise
    at parse time either; the gate surfaces an unknown backend separately."""
    assert parse_model_policy({"model_policy": "nonsense"}).backends == {}
    assert parse_model_policy({"model_policy": {"backends": "nope"}}).backends == {}
    # A backend entry with no models is skipped rather than allowing everything.
    policy = parse_model_policy({"model_policy": {"backends": {"cmd": {}}}})
    assert policy.backend("cmd") is None


def test_empty_roles_list_means_any_role() -> None:
    policy = parse_model_policy(
        {"model_policy": {"backends": {"cmd": {"allowed_models": ["m"], "roles": []}}}}
    )
    assert policy.check(backend="cmd", model="m", role="judge") == "m"


def test_shipped_example_config_matches_the_owner_policy() -> None:
    """examples/fleet.gate.yaml must encode exactly the approved policy."""
    example = Path(__file__).resolve().parent.parent / "examples" / "fleet.gate.yaml"
    raw = yaml.safe_load(example.read_text(encoding="utf-8"))
    policy = parse_model_policy(raw)
    assert policy.backend("cmd") is not None
    assert policy.backend("cmd").allowed_models == frozenset({"stealth/space-bunny-alpha"})
    assert policy.backend("cmd").roles is None  # cmd may serve any gate role
    grok = policy.backend("grok")
    assert grok is not None
    assert grok.allowed_models == frozenset({"step-5-preview"})
    assert grok.roles == frozenset({"judge"})


def test_backend_policy_check_model_returns_the_model() -> None:
    bp = BackendPolicy(name="cmd", allowed_models=frozenset({"m"}))
    assert bp.check_model("m") == "m"
    with pytest.raises(ModelPolicyError):
        bp.check_model("other")


def test_backend_policy_check_role_allows_when_unset() -> None:
    bp = BackendPolicy(name="cmd", allowed_models=frozenset({"m"}), roles=None)
    bp.check_role("anything")  # must not raise


def test_empty_model_policy_object_allows_unpinned_backends() -> None:
    policy = ModelPolicy(backends={})
    assert policy.backend("nope") is None
    assert policy.check(backend="nope", model="m", role="lens") == "m"
