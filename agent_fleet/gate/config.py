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
)

_STRINGS: tuple[str, ...] = (
    "backend",
    "judge_backend",
    "base_branch",
    "test_memory",
)

_OPTIONAL_STRINGS: tuple[str, ...] = ("model", "judge_model", "push_branch", "package_dir")

_BOOLS: tuple[str, ...] = ("enable_fix", "enable_judge")


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

    def focus_for(self, lens: str) -> str:
        """Reviewer focus text for *lens* (custom or default)."""
        return self.lens_focus.get(lens) or DEFAULT_LENSES.get(lens) or lens


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
    return GateConfig(**kwargs)
