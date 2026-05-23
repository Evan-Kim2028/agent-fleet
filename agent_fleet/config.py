"""Fleet configuration loader."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_DEFAULT_CONFIG_PATH = Path.home() / ".hermes" / "coding_fleet" / "fleet.yaml"
_PACKAGE_PERSONAS = Path(__file__).resolve().parent / "personas"

_DEFAULT_PIPELINES: dict[str, list[str]] = {
    "simple": ["execute"],
    "code_review": ["execute", "review"],
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


@dataclass
class FleetConfig:
    default_model: str = "composer-2.5"
    default_mode: str = "agent"
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
    repo_config: Any = None


def _expand_path(value: str) -> Path:
    return Path(os.path.expanduser(value)).resolve()


def _parse_persona_specs(raw: dict[str, Any]) -> dict[str, PersonaSpec]:
    specs: dict[str, PersonaSpec] = {}
    for name, entry in (raw or {}).items():
        if isinstance(entry, str):
            specs[name] = PersonaSpec(prompt=entry)
            continue
        if not isinstance(entry, dict):
            continue
        specs[name] = PersonaSpec(
            prompt=str(entry.get("prompt") or f"{name}.md"),
            model=entry.get("model"),
            mode=entry.get("mode"),
            skill=entry.get("skill"),
            allowed_paths=list(entry.get("allowed_paths") or []),
            extra_instructions=str(entry.get("extra_instructions") or ""),
        )
    return specs


def load_fleet_config(
    path: Path | str | None = None,
    **overrides: Any,
) -> FleetConfig:
    config_path = _expand_path(str(path)) if path else _DEFAULT_CONFIG_PATH
    data: dict[str, Any] = {}
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            data = loaded

    personas_dir_raw = overrides.get("personas_dir") or data.get("personas_dir") or _PACKAGE_PERSONAS
    personas_path = Path(str(personas_dir_raw))
    if not personas_path.is_absolute():
        base = config_path.parent if config_path.exists() else Path(__file__).resolve().parent.parent
        personas_dir = (base / personas_path).resolve()
    else:
        personas_dir = _expand_path(str(personas_dir_raw))
    skill_dirs = [
        _expand_path(str(p))
        for p in (overrides.get("skill_dirs") or data.get("skill_dirs") or [])
    ]
    default_skill_dir = Path.home() / ".hermes" / "skills"
    if default_skill_dir.exists() and default_skill_dir not in skill_dirs:
        skill_dirs.append(default_skill_dir)

    cfg = FleetConfig(
        default_model=str(
            overrides.get("default_model") or data.get("default_model") or "composer-2.5"
        ),
        default_mode=str(
            overrides.get("default_mode") or data.get("default_mode") or "agent"
        ),
        default_backend=str(
            overrides.get("default_backend") or data.get("default_backend") or "cursor"
        ),
        kimi_bin=overrides.get("kimi_bin") or data.get("kimi_bin"),
        max_parallel=int(
            overrides.get("max_parallel") or data.get("max_parallel") or 3
        ),
        timeout_seconds=int(
            overrides.get("timeout_seconds") or data.get("timeout_seconds") or 900
        ),
        ram_budget_gb=int(
            overrides.get("ram_budget_gb") or data.get("ram_budget_gb") or 24
        ),
        personas_dir=personas_dir,
        skill_dirs=skill_dirs,
        personas=_parse_persona_specs(data.get("personas") or {}),
        pipelines={**_DEFAULT_PIPELINES, **dict(data.get("pipelines") or {})},
        default_pipeline=str(
            overrides.get("default_pipeline")
            or data.get("default_pipeline")
            or "simple"
        ),
        default_persona=str(
            overrides.get("default_persona") or data.get("default_persona") or "coder"
        ),
        default_workspace=overrides.get("default_workspace")
        or data.get("default_workspace"),
    )
    return cfg
