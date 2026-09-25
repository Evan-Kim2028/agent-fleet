"""Model policy: which models each backend may use, and for which roles.

A ``fleet.yaml`` ``model_policy`` section pins the models the gate pipeline is
allowed to dispatch, so a typo or a config drift cannot quietly spend a different
model than the one the operator approved::

    model_policy:
      backends:
        cmd:
          allowed_models: ["stealth/space-bunny-alpha"]
        grok:
          allowed_models: ["step-5-preview"]
          roles: ["judge"]

``roles`` restricts a backend to specific pipeline roles; a backend listed
without ``roles`` may serve any role. :func:`check_model_allowed` raises
:class:`ModelPolicyError` before any agent is dispatched, which is the point —
the gate must not burn a call on a model the policy forbids.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ModelPolicyError(RuntimeError):
    """Raised when a backend/model/role combination violates the model policy."""


@dataclass(frozen=True)
class BackendPolicy:
    """Allowed models for one backend, and the roles it may serve."""

    name: str
    allowed_models: frozenset[str]
    roles: frozenset[str] | None = None

    def check_model(self, model: str | None) -> str:
        """Return the effective model, raising if the policy forbids it."""
        if not model:
            raise ModelPolicyError(
                f"model_policy: backend {self.name!r} requires an explicit model; "
                f"allowed: {sorted(self.allowed_models)}"
            )
        if model not in self.allowed_models:
            raise ModelPolicyError(
                f"model_policy: model {model!r} not allowed for backend {self.name!r}; "
                f"allowed: {sorted(self.allowed_models)}"
            )
        return model

    def check_role(self, role: str, aliases: tuple[str, ...] = ()) -> None:
        """Raise if this backend is not permitted to serve *role*.

        *aliases* are additional names for the same dispatch. A role may be
        spelled either way without breaking a policy: the gate's find step is the
        ``find`` role under ``gate.roles`` but the ``lens`` role in the policy
        vocabulary, and both name the same call.
        """
        if self.roles is None:
            return
        if not (self.roles & {role, *aliases}):
            raise ModelPolicyError(
                f"model_policy: backend {self.name!r} may not serve role {role!r}; "
                f"allowed roles: {sorted(self.roles)}"
            )


@dataclass(frozen=True)
class ModelPolicy:
    """Resolved ``model_policy`` section. Empty policy allows everything."""

    backends: dict[str, BackendPolicy]

    def backend(self, name: str) -> BackendPolicy | None:
        return self.backends.get(name.lower())

    def check(
        self,
        *,
        backend: str,
        model: str | None,
        role: str,
        aliases: tuple[str, ...] = (),
    ) -> str:
        """Validate one dispatch and return the model to use.

        A backend absent from the policy is unrestricted (an operator who pins
        some backends has not necessarily pinned all of them).
        """
        policy = self.backend(backend)
        if policy is None:
            if not model:
                raise ModelPolicyError(f"model_policy: no model given for backend {backend!r}")
            return model
        policy.check_role(role, aliases)
        return policy.check_model(model)


def parse_model_policy(raw: dict[str, Any] | None) -> ModelPolicy:
    """Build a :class:`ModelPolicy` from the ``model_policy`` config section."""
    section = (raw or {}).get("model_policy")
    if not section or not isinstance(section, dict):
        return ModelPolicy(backends={})
    backends_raw = section.get("backends")
    if not backends_raw or not isinstance(backends_raw, dict):
        return ModelPolicy(backends={})

    backends: dict[str, BackendPolicy] = {}
    for name, spec in backends_raw.items():
        if not isinstance(spec, dict):
            continue
        models = spec.get("allowed_models") or []
        if not isinstance(models, list) or not models:
            continue
        roles_raw = spec.get("roles")
        roles = (
            frozenset(str(r) for r in roles_raw)
            if isinstance(roles_raw, list) and roles_raw
            else None
        )
        backends[str(name).lower()] = BackendPolicy(
            name=str(name).lower(),
            allowed_models=frozenset(str(m) for m in models),
            roles=roles,
        )
    return ModelPolicy(backends=backends)
