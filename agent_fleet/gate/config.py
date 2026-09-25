"""Gate pipeline configuration from .agent-fleet.yaml.

The gate is configured per-repo under a ``gate:`` section, with the machine-wide
model policy coming from the global ``fleet.yaml`` (``model_policy:``) so every
lane on the box shares one approved-model list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Each lens is one reviewer focus. The four defaults mirror the lanes the
# reference gate ran: correctness, contract, production safety, and spec
# conformance. A repo can add or replace lenses via ``gate.lenses``.
DEFAULT_LENSES: dict[str, str] = {
    "correctness": (
        "Logic errors, wrong results, broken edge cases, error handling, "
        "concurrency/race bugs, regressions of existing behaviour."
    ),
    "contract": (
        "Public API/MCP/schema contract breaks, response shape, params, "
        "docs/OpenAPI drift vs behaviour, backwards compatibility, missing "
        "tests for promised behaviour."
    ),
    "prodsafety": (
        "Production data safety and ops: data loss, destructive writes, "
        "unbounded memory/CPU on the VPS, missing locks/atomicity, unsafe "
        "migrations, performance regressions on hot paths, secrets exposure."
    ),
    "spec": (
        "Conformance to the task specification below: required items missing "
        "or implemented differently from what was asked."
    ),
}

DEFAULT_LENS_ORDER: tuple[str, ...] = ("correctness", "contract", "prodsafety", "spec")

_SCALARS: tuple[str, ...] = (
    "max_findings",
    "max_candidates",
    "agent_timeout_s",
    "judge_timeout_s",
    "test_timeout_s",
    "max_parallel_lenses",
    "max_parallel_verifiers",
    "max_fix_rounds",
    "agent_slots",
    "test_slots",
    "openrouter_slots",
    "inline_diff_chars",
    "inline_file_chars",
    "inline_total_chars",
)

_STRINGS: tuple[str, ...] = (
    "backend",
    "judge_backend",
    "base_branch",
    "test_memory",
)

_OPTIONAL_STRINGS: tuple[str, ...] = ("model", "judge_model", "push_branch", "package_dir")

_BOOLS: tuple[str, ...] = ("enable_fix", "enable_judge")

_ROLES: tuple[str, ...] = ("find", "judge", "verify", "fix")

# Per-role fallback to the single global keys. ``find``/``verify``/``fix`` share
# ``backend``/``model``; ``judge`` has always had its own pair. A role absent from
# ``gate.roles`` resolves through this map, so a config written before per-role
# backends existed behaves exactly as it did.
_ROLE_FALLBACK: dict[str, tuple[str, str]] = {
    "find": ("backend", "model"),
    "verify": ("backend", "model"),
    "fix": ("backend", "model"),
    "judge": ("judge_backend", "judge_model"),
}

# A backend that gets the change inlined into the prompt instead of being trusted
# to read the repo itself. OpenRouter reviews see the diff and the changed files
# pasted in; see agent_fleet/gate/inline.py.
_NO_TOOL_BACKENDS: frozenset[str] = frozenset({"openrouter"})


@dataclass(frozen=True)
class RoleTarget:
    """Which backend and model serves one gate role."""

    backend: str
    model: str | None = None

    @property
    def needs_inline_context(self) -> bool:
        """True when this backend cannot be relied on to read the repo itself."""
        return self.backend.lower() in _NO_TOOL_BACKENDS


@dataclass(frozen=True)
class GateConfig:
    """Settings for the ``agent-fleet gate`` PR pipeline."""

    lenses: tuple[str, ...] = DEFAULT_LENS_ORDER
    lens_focus: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_LENSES))
    backend: str = "cmd"
    model: str | None = None
    judge_backend: str = "cmd"  # owner policy: judge on cmd/space-bunny; grok is opt-in only
    judge_model: str | None = None
    max_findings: int = 12
    max_candidates: int = 12
    base_branch: str = "main"
    push_branch: str | None = None
    enable_fix: bool = True
    enable_judge: bool = True
    agent_timeout_s: int = 1800
    judge_timeout_s: int = 7200
    test_timeout_s: int = 900
    max_parallel_lenses: int = 8
    max_parallel_verifiers: int = 6
    test_memory: str = "6G"
    package_dir: str | None = None
    # Safety net, not the stopping rule. The fix loop continues while the
    # failing set strictly shrinks; ``max_fix_rounds`` only bounds a run that
    # keeps making microscopic progress. See docs/GATE.md.
    max_fix_rounds: int = 4
    agent_slots: int = 24
    test_slots: int = 4
    # Remote (OpenRouter) calls are admitted from their own pool so they never
    # queue behind — or consume — the local cmd agent budget.
    openrouter_slots: int = 32
    # Caps on the change pasted into a no-tool backend's prompt. The evidence is
    # inlined rather than read by the model, so it must be bounded.
    inline_diff_chars: int = 60_000
    inline_file_chars: int = 20_000
    inline_total_chars: int = 120_000
    # Per-role backend/model overrides. A role absent here resolves through
    # ``_ROLE_FALLBACK`` to the single global keys, so existing configs are
    # unaffected.
    roles: dict[str, RoleTarget] = field(default_factory=dict)

    def focus_for(self, lens: str) -> str:
        """Reviewer focus text for *lens* (custom or default)."""
        return self.lens_focus.get(lens) or DEFAULT_LENSES.get(lens) or lens

    def role_target(self, role: str) -> RoleTarget:
        """Backend + model for one gate *role*, falling back to the global keys.

        This is the single place a role's dispatch target is resolved, so the
        config parser, the pre-dispatch policy check, and the pipeline's dispatch
        can never disagree about where a role runs.
        """
        override = self.roles.get(role)
        if override is not None:
            return override
        backend_key, model_key = _ROLE_FALLBACK.get(role, ("backend", "model"))
        return RoleTarget(backend=getattr(self, backend_key), model=getattr(self, model_key))


def load_gate_config(raw: dict[str, Any] | None) -> GateConfig | None:
    """Load the ``gate:`` section; return ``None`` when the gate is disabled.

    ``gate: false`` disables it explicitly, matching the ``code_review: false``
    convention. Any other absent/malformed section yields the defaults, so a CLI
    ``gate`` run works in a repo with no config at all.
    """
    section = (raw or {}).get("gate")
    if section is False:
        return None
    defaults = GateConfig()
    if not section or not isinstance(section, dict):
        return defaults

    focus = dict(defaults.lens_focus)
    lenses = defaults.lenses
    lenses_raw = section.get("lenses")
    if isinstance(lenses_raw, dict) and lenses_raw:
        # Mapping form: custom lens name -> focus text replaces the defaults.
        focus = {str(k): str(v) for k, v in lenses_raw.items()}
        lenses = tuple(focus)
    elif isinstance(lenses_raw, list) and lenses_raw:
        lenses = tuple(str(name) for name in lenses_raw)
    extra_focus = section.get("lens_focus")
    if isinstance(extra_focus, dict):
        focus.update({str(k): str(v) for k, v in extra_focus.items()})

    kwargs: dict[str, Any] = {
        "lenses": lenses,
        "lens_focus": focus,
        "enable_fix": bool(section.get("enable_fix", defaults.enable_fix)),
        "enable_judge": bool(section.get("enable_judge", defaults.enable_judge)),
    }
    for key in _SCALARS:
        kwargs[key] = int(section.get(key, getattr(defaults, key)))
    for key in _STRINGS:
        kwargs[key] = str(section.get(key) or getattr(defaults, key))
    for key in _OPTIONAL_STRINGS:
        value = section.get(key)
        kwargs[key] = str(value) if value else getattr(defaults, key)
    for key in _BOOLS:
        kwargs[key] = bool(section.get(key, getattr(defaults, key)))
    # Parsed last, so a role entry that names no model inherits the *resolved*
    # global model rather than the dataclass default.
    kwargs["roles"] = _parse_roles(section.get("roles"), kwargs)
    return GateConfig(**kwargs)


def _parse_roles(raw: Any, resolved: dict[str, Any]) -> dict[str, RoleTarget]:  # noqa: ANN401
    """Parse ``gate.roles:`` into per-role targets.

    A role is only overridden when it names a backend; a role entry that omits
    ``backend`` is ignored rather than silently resolving to an empty backend
    name, so a half-written entry falls back to the global keys instead of
    breaking the run with an unroutable backend. A role that names a backend but
    no model inherits the resolved fallback model for that role.
    """
    if not isinstance(raw, dict) or not raw:
        return {}
    out: dict[str, RoleTarget] = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            continue
        backend = str(spec.get("backend") or "").strip()
        if not backend:
            continue
        model = spec.get("model")
        if not model:
            model = _ROLE_FALLBACK.get(str(name), ("backend", "model"))[1]
            model = resolved.get(model)
        out[str(name)] = RoleTarget(backend=backend, model=str(model) if model else None)
    return out
