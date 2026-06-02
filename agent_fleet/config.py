"""Fleet configuration loader."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from agent_fleet.agent_mode import coerce_agent_mode
from agent_fleet.contracts.mcp import McpServerSpec, parse_mcp_server_spec
from agent_fleet.fleet_paths import (
    default_fleet_config_path,
    default_runs_dir,
    user_skill_dir,
)
from agent_fleet.persona_router import PersonaRoutingConfig, parse_persona_routing
from agent_fleet.skills_lib import bundled_skill_dirs, merge_skill_dirs

if TYPE_CHECKING:
    from agent_fleet.agent_mode import AgentMode
    from agent_fleet.repo import RepoConfig

_PACKAGE_PERSONAS = Path(__file__).resolve().parent / "personas"

__all__ = [
    "FleetConfig",
    "PersonaSpec",
    "default_fleet_config_path",
    "default_runs_dir",
    "load_fleet_config",
]


_DEFAULT_PIPELINES: dict[str, list[str]] = {
    "simple": ["execute"],
    "code_review": ["execute", "review"],
    "pr_review": ["analyze"],
    "full": ["plan", "research", "synthesize", "implement", "verify", "review"],
}


@dataclass
class PersonaSpec:
    prompt: str
    model: str | None = None
    mode: str | None = None
    skill: str | None = None
    allowed_paths: list[str] = field(default_factory=list)
    extra_instructions: str = ""
    mcp_servers: list[str] = field(default_factory=list)


@dataclass
class FleetConfig:
    default_model: str = "composer-2.5"
    default_mode: AgentMode = "agent"
    default_backend: str = "cursor"
    kimi_bin: str | None = None
    default_persona: str = "coder"
    max_parallel: int = 3
    timeout_seconds: int = 900
    ram_budget_gb: int = 24
    personas_dir: Path = field(default_factory=lambda: _PACKAGE_PERSONAS)
    skill_dirs: list[Path] = field(default_factory=list)
    personas: dict[str, PersonaSpec] = field(default_factory=dict)
    pipelines: dict[str, list[str]] = field(default_factory=lambda: dict(_DEFAULT_PIPELINES))
    default_pipeline: str = "simple"
    default_workspace: str | None = None
    repo_config: RepoConfig | None = None
    mcp_servers: dict[str, McpServerSpec] = field(default_factory=dict)
    max_redispatches: int = 1
    enforce_token_ceiling: bool = False
    persona_routing: PersonaRoutingConfig | None = None
    # Per-tier runtime overrides: merged over the Python _RUNTIME_MAP defaults.
    # Keys are "LOW"/"MED"/"HIGH"; values are partial RuntimeConfig field dicts.
    complexity_tiers: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Skill-set overrides: keys "minimal_core" (list[str]) and "pr_loop" (list[str]).
    skill_overrides: dict[str, list[str]] = field(default_factory=dict)


def _expand_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


_ENV_VAR_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand_env(value: Any) -> Any:  # noqa: ANN401
    """Recursively expand ${VAR} occurrences in strings inside dicts/lists."""
    if isinstance(value, str):

        def _sub(match: re.Match[str]) -> str:
            var = match.group(1)
            if var not in os.environ:
                raise ValueError(f"environment variable {var!r} required but not set")
            return os.environ[var]

        return _ENV_VAR_RE.sub(_sub, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _parse_mcp_catalog(raw: dict[str, Any]) -> dict[str, McpServerSpec]:
    catalog: dict[str, McpServerSpec] = {}
    for name, entry in (raw or {}).items():
        if not isinstance(entry, dict):
            continue
        expanded = _expand_env(entry)
        catalog[name] = parse_mcp_server_spec(name, expanded)
    return catalog


def _parse_persona_specs(
    raw: dict[str, Any], catalog: dict[str, McpServerSpec]
) -> dict[str, PersonaSpec]:
    specs: dict[str, PersonaSpec] = {}
    for name, entry in (raw or {}).items():
        if isinstance(entry, str):
            specs[name] = PersonaSpec(prompt=entry)
            continue
        if not isinstance(entry, dict):
            continue
        mcp_names = list(entry.get("mcp_servers") or [])
        for mcp_name in mcp_names:
            if mcp_name not in catalog:
                raise ValueError(
                    f"persona {name!r} references unknown MCP server {mcp_name!r}; "
                    f"known: {sorted(catalog)}"
                )
        specs[name] = PersonaSpec(
            prompt=str(entry.get("prompt") or f"{name}.md"),
            model=entry.get("model"),
            mode=entry.get("mode"),
            skill=entry.get("skill"),
            allowed_paths=list(entry.get("allowed_paths") or []),
            extra_instructions=str(entry.get("extra_instructions") or ""),
            mcp_servers=mcp_names,
        )
    return specs


_VALID_TIER_KEYS = frozenset({"LOW", "MED", "HIGH"})
_VALID_TIER_FIELDS = frozenset({"pipeline", "retries", "token_ceiling", "loadout_size"})


def _parse_complexity_tiers(raw: Any) -> dict[str, dict[str, Any]]:  # noqa: ANN401
    """Parse ``complexity_tiers`` YAML block into per-tier override dicts.

    Unknown tier names (not LOW/MED/HIGH) are silently ignored for forward
    compatibility.  Unknown field keys within a known tier raise ``ValueError``
    so typos are surfaced immediately rather than silently dropped.
    """
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for tier, fields in raw.items():
        key = str(tier).strip().upper()
        if key not in _VALID_TIER_KEYS or not isinstance(fields, dict):
            continue
        unknown = set(fields) - _VALID_TIER_FIELDS
        if unknown:
            raise ValueError(
                f"complexity_tiers[{key!r}] contains unknown field(s) {sorted(unknown)}; "
                f"valid fields: {sorted(_VALID_TIER_FIELDS)}"
            )
        result[key] = dict(fields)
    return result


def _parse_skill_overrides(raw: Any) -> dict[str, list[str]]:  # noqa: ANN401
    """Parse ``skills`` YAML block into a dict with optional ``minimal_core``
    and ``pr_loop`` lists.
    """
    if not isinstance(raw, dict):
        return {}
    result: dict[str, list[str]] = {}
    for key in ("minimal_core", "pr_loop"):
        value = raw.get(key)
        if isinstance(value, list):
            result[key] = [str(s) for s in value]
    return result


def load_fleet_config(
    path: Path | str | None = None,
    *,
    personas_dir: Path | str | None = None,
    skill_dirs: list[Path | str] | None = None,
    default_model: str | None = None,
    default_mode: str | None = None,
    default_backend: str | None = None,
    kimi_bin: str | None = None,
    max_parallel: int | None = None,
    timeout_seconds: int | None = None,
    ram_budget_gb: int | None = None,
    default_pipeline: str | None = None,
    default_persona: str | None = None,
    default_workspace: str | None = None,
) -> FleetConfig:
    config_path = _expand_path(str(path)) if path else default_fleet_config_path()
    data: dict[str, Any] = {}
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            data = loaded

    personas_dir_raw = personas_dir or data.get("personas_dir") or _PACKAGE_PERSONAS
    personas_path = Path(str(personas_dir_raw))
    if not personas_path.is_absolute():
        base = (
            config_path.parent if config_path.exists() else Path(__file__).resolve().parent.parent
        )
        personas_dir_resolved = (base / personas_path).resolve()
    else:
        personas_dir_resolved = _expand_path(str(personas_dir_raw))
    skill_dirs_resolved = merge_skill_dirs(
        bundled_skill_dirs(),
        [_expand_path(str(p)) for p in (skill_dirs or data.get("skill_dirs") or [])],
    )
    skill_home = user_skill_dir()
    if skill_home.exists():
        skill_dirs_resolved = merge_skill_dirs(skill_dirs_resolved, [skill_home])

    mcp_catalog = _parse_mcp_catalog(data.get("mcp_servers") or {})

    return FleetConfig(
        default_model=str(default_model or data.get("default_model") or "composer-2.5"),
        default_mode=coerce_agent_mode(str(default_mode or data.get("default_mode") or "agent")),
        default_backend=str(default_backend or data.get("default_backend") or "cursor"),
        kimi_bin=kimi_bin or data.get("kimi_bin"),
        max_parallel=int(max_parallel or data.get("max_parallel") or 3),
        timeout_seconds=int(timeout_seconds or data.get("timeout_seconds") or 900),
        ram_budget_gb=int(ram_budget_gb or data.get("ram_budget_gb") or 24),
        personas_dir=personas_dir_resolved,
        skill_dirs=skill_dirs_resolved,
        personas=_parse_persona_specs(data.get("personas") or {}, mcp_catalog),
        pipelines={**_DEFAULT_PIPELINES, **dict(data.get("pipelines") or {})},
        default_pipeline=str(default_pipeline or data.get("default_pipeline") or "simple"),
        default_persona=str(default_persona or data.get("default_persona") or "coder"),
        default_workspace=default_workspace or data.get("default_workspace"),
        mcp_servers=mcp_catalog,
        max_redispatches=int(data.get("max_redispatches") or 1),
        enforce_token_ceiling=bool(data.get("enforce_token_ceiling", False)),
        persona_routing=parse_persona_routing(data.get("persona_routing")),
        complexity_tiers=_parse_complexity_tiers(data.get("complexity_tiers")),
        skill_overrides=_parse_skill_overrides(data.get("skills")),
    )
