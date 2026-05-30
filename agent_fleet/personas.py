"""Persona resolution from markdown prompts, YAML registry, loadouts, and skills."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

import yaml

from agent_fleet.config import FleetConfig, PersonaSpec, load_fleet_config
from agent_fleet.hooks import Persona
from agent_fleet.level_up.models import LevelUpOverlay
from agent_fleet.level_up.overlay import compose_overlay_text, load_overlay
from agent_fleet.level_up.paths import repo_key as level_up_repo_key
from agent_fleet.skills_lib import (
    base_kit_skill_dirs,
    compose_persona_body,
    find_skill_path,
    merge_skill_dirs,
)

_DEFAULT_ALLOWED_TOOLS = ["read_file", "write_file", "run_command"]
_PACKAGE_PERSONAS = Path(__file__).resolve().parent / "personas"
_REFERENCE_DELIMITER = "\n\n---\n\n"
_logger = logging.getLogger(__name__)


def load_persona_md(
    name: str,
    *,
    personas_dir: Path | None = None,
    loadout_size: Literal["minimal", "standard", "full"] = "standard",
) -> str:
    """Return the markdown prompt for *name* honoring *loadout_size*.

    New-layout personas live in ``personas/<name>/loadout.md`` with an optional
    ``reference/`` subdirectory.  Legacy flat personas are ``personas/<name>.md``.

    Sizes:
    - ``minimal``: loadout.md only.
    - ``standard``: loadout.md + reference/INDEX.md (agent decides what else to fetch).
    - ``full``: loadout.md + reference/INDEX.md + first paragraph of each reference doc.

    Legacy flat personas return the flat file for all sizes.  A warning is logged
    when ``loadout_size != "minimal"`` so unmigrated personas are visible.
    """
    bases: list[Path] = []
    if personas_dir is not None:
        bases.append(personas_dir)
    if personas_dir is None or personas_dir.resolve() != _PACKAGE_PERSONAS.resolve():
        bases.append(_PACKAGE_PERSONAS)

    for base in bases:
        persona_dir = base / name
        if persona_dir.is_dir():
            loadout_path = persona_dir / "loadout.md"
            if loadout_path.is_file():
                return _load_directory_persona(persona_dir, loadout_path, loadout_size)

    # Legacy flat layout
    for base in bases:
        flat_path = base / f"{name}.md"
        if flat_path.is_file():
            if loadout_size != "minimal":
                _logger.warning(
                    "persona %r is a legacy flat file; loadout_size=%r ignored "
                    "(migrate to personas/%s/ to enable reference docs)",
                    name,
                    loadout_size,
                    name,
                )
            return flat_path.read_text(encoding="utf-8")

    raise FileNotFoundError(f"Persona {name!r} not found in any of: {[str(b) for b in bases]}")


def _load_directory_persona(
    persona_dir: Path,
    loadout_path: Path,
    loadout_size: Literal["minimal", "standard", "full"],
) -> str:
    """Assemble persona markdown from directory layout."""
    parts: list[str] = [loadout_path.read_text(encoding="utf-8")]

    if loadout_size == "minimal":
        return parts[0]

    reference_dir = persona_dir / "reference"
    index_path = reference_dir / "INDEX.md"
    if index_path.is_file():
        parts.append(index_path.read_text(encoding="utf-8"))

    if loadout_size == "full" and reference_dir.is_dir():
        for ref_path in sorted(reference_dir.glob("*.md")):
            if ref_path.name == "INDEX.md":
                continue
            first_para = _first_paragraph(ref_path.read_text(encoding="utf-8"))
            if first_para:
                header = f"## {ref_path.stem} (excerpt)"
                parts.append(f"{header}\n\n{first_para}")

    return _REFERENCE_DELIMITER.join(parts)


def _first_paragraph(text: str) -> str:
    """Return the first non-empty paragraph of *text*."""
    paragraphs = text.split("\n\n")
    for para in paragraphs:
        stripped = para.strip()
        if stripped:
            return stripped
    return ""


def _persona_search_dirs(cfg: FleetConfig) -> tuple[Path, ...]:
    """Search repo/local personas_dir first, then bundled package personas."""
    primary = cfg.personas_dir
    if primary.resolve() == _PACKAGE_PERSONAS.resolve():
        return (primary,)
    return (primary, _PACKAGE_PERSONAS)


def read_persona_body(persona: Persona) -> str:
    """Return the composed persona prompt (loadout + overlays) or prompt file contents."""
    if persona.body is not None:
        return persona.body
    return persona.prompt_path.read_text(encoding="utf-8")


def load_loadout(name: str, *, personas_dir: Path | None = None) -> dict[str, Any] | None:
    bases: list[Path] = []
    if personas_dir is not None:
        bases.append(personas_dir)
    if personas_dir is None or personas_dir.resolve() != _PACKAGE_PERSONAS.resolve():
        bases.append(_PACKAGE_PERSONAS)
    for base in bases:
        path = base / f"{name}.loadout.yaml"
        if not path.is_file():
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Invalid loadout {path}: expected mapping")
        return data
    return None


def _loadout_stub_path(
    loadout: dict[str, Any],
    personas_dirs: tuple[Path, ...],
) -> Path | None:
    stub = loadout.get("stub")
    if not stub:
        return None
    for personas_dir in personas_dirs:
        stub_path = personas_dir / str(stub)
        if stub_path.is_file():
            return stub_path.resolve()
    return None


def _repo_key(cfg: FleetConfig) -> str | None:
    repo = cfg.repo_config
    if repo is None:
        return None
    return level_up_repo_key(name=repo.name, repo_root=repo.repo_root)


def _level_up_overlay_texts(repo_key: str | None, persona: str) -> tuple[str, str, int]:
    fleet_overlay = load_overlay("_fleet", persona)
    repo_overlay = (
        load_overlay(repo_key, persona)
        if repo_key
        else LevelUpOverlay(schema_version=1, rules=(), generation=0)
    )
    fleet_text = compose_overlay_text(fleet_overlay.rules)
    repo_text = compose_overlay_text(repo_overlay.rules)
    generation = max(fleet_overlay.generation, repo_overlay.generation)
    return fleet_text, repo_text, generation


def _resolve_prompt_path(spec: PersonaSpec, cfg: FleetConfig) -> Path:
    prompt = spec.prompt.strip()
    if prompt.startswith(("/", "~")):
        return Path(prompt).expanduser().resolve()
    direct = Path(prompt)
    if direct.is_absolute() and direct.exists():
        return direct
    if direct.exists():
        return direct.resolve()
    for personas_dir in _persona_search_dirs(cfg):
        in_personas = personas_dir / prompt
        if in_personas.exists():
            return in_personas.resolve()
        if not prompt.endswith(".md"):
            with_suffix = personas_dir / f"{prompt}.md"
            if with_suffix.exists():
                return with_suffix.resolve()
    if spec.skill:
        skill_path = find_skill_path(spec.skill, cfg.skill_dirs)
        if skill_path:
            return skill_path
    searched = ", ".join(str(path) for path in _persona_search_dirs(cfg))
    raise ValueError(
        f"Persona prompt not found: {prompt!r} "
        f"(searched personas_dirs=[{searched}], skill={spec.skill!r})"
    )


def _loadout_skill_slots(loadout: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    skill_slots = loadout.get("skill_slots")
    if isinstance(skill_slots, dict):
        execute = tuple(str(skill_id) for skill_id in (skill_slots.get("execute") or []))
        review = tuple(str(skill_id) for skill_id in (skill_slots.get("review") or []))
        return execute, review
    skills = loadout.get("skills") or {}
    execute = tuple(str(skill_id) for skill_id in (skills.get("execute") or []))
    pipeline_skills = loadout.get("pipeline_skills") or {}
    code_review = pipeline_skills.get("code_review") or {}
    review = tuple(str(skill_id) for skill_id in (code_review.get("review") or []))
    return execute, review


class YamlPersonaResolver:
    """Load personas from fleet.yaml, loadouts, markdown/skill files, and level-up overlays."""

    def __init__(self, config: FleetConfig) -> None:
        self._config = config

    def list_personas(self) -> list[str]:
        names = set(self._config.personas.keys())
        for personas_dir in _persona_search_dirs(self._config):
            if not personas_dir.exists():
                continue
            for path in sorted(personas_dir.glob("*.md")):
                names.add(path.stem)
            for path in sorted(personas_dir.glob("*.loadout.yaml")):
                names.add(path.stem.replace(".loadout", ""))
        return sorted(names)

    def load(self, name: str, *, loadout_size: str | None = None) -> Persona:  # noqa: ARG002
        """Load a persona by name.

        *loadout_size* is accepted for forward-compatibility with complexity-
        driven runtime derivation.  The actual loadout selection logic is
        unchanged and will be updated by a parallel subagent.
        """
        spec = self._config.personas.get(name)
        if spec is None:
            spec = PersonaSpec(prompt=f"{name}.md")

        loadout = load_loadout(name, personas_dir=self._config.personas_dir)
        skill_slots_execute: tuple[str, ...] = ()
        skill_slots_review: tuple[str, ...] = ()
        level_up_generation = 0
        body: str | None = None
        prompt_path: Path

        if loadout is not None:
            skill_dirs = merge_skill_dirs(
                base_kit_skill_dirs(),
                self._config.skill_dirs,
            )
            repo_key = _repo_key(self._config)
            fleet_overlay, repo_overlay, level_up_generation = _level_up_overlay_texts(
                repo_key,
                name,
            )
            stub_path = _loadout_stub_path(loadout, _persona_search_dirs(self._config))
            stub_text = (
                stub_path.read_text(encoding="utf-8").strip() if stub_path is not None else None
            )
            body = compose_persona_body(
                loadout,
                fleet_overlay=fleet_overlay,
                repo_overlay=repo_overlay,
                stub_text=stub_text,
                skill_dirs=skill_dirs,
                level_up_generation=level_up_generation,
            )
            skill_slots_execute, skill_slots_review = _loadout_skill_slots(loadout)
            if stub_path is not None:
                prompt_path = stub_path
            elif spec.skill:
                skill_path = find_skill_path(spec.skill, skill_dirs)
                prompt_path = (
                    skill_path
                    if skill_path is not None
                    else _resolve_prompt_path(spec, self._config)
                )
            else:
                prompt_path = _resolve_prompt_path(spec, self._config)
        else:
            prompt_path = _resolve_prompt_path(spec, self._config)

        allowed_paths = tuple(spec.allowed_paths)
        repo = self._config.repo_config
        if repo and name in repo.persona_scope_allowlist:
            allowed_paths = repo.persona_scope_allowlist[name]
        allowed_tools = list(_DEFAULT_ALLOWED_TOOLS)
        for path_glob in allowed_paths:
            allowed_tools.append(f"path:{path_glob}")
        capabilities: dict[str, bool] = {"unrestricted_scope": len(allowed_paths) == 0}
        for path_glob in allowed_paths:
            capabilities[f"scope:{path_glob}"] = True
        return Persona(
            name=name,
            prompt_path=prompt_path,
            body=body,
            skill_slots_execute=skill_slots_execute,
            skill_slots_review=skill_slots_review,
            level_up_generation=level_up_generation,
            allowed_tools=allowed_tools,
            capabilities=capabilities,
            allowed_paths=allowed_paths,
            model=spec.model or self._config.default_model,
            mode=spec.mode or self._config.default_mode,
            extra_instructions=spec.extra_instructions,
            mcp_servers=list(spec.mcp_servers),
        )


def persona_prompt_resolves(_name: str, spec: PersonaSpec, cfg: FleetConfig) -> bool:
    """Return True when the persona prompt resolves to an existing markdown or skill file."""
    try:
        _resolve_prompt_path(spec, cfg)
    except ValueError:
        return False
    return True


def prune_fleet_yaml_personas(path: Path) -> list[str]:
    """Remove fleet.yaml persona entries whose prompt .md is missing from all search dirs.

    Returns the list of pruned persona names. Rewrites *path* only when entries are removed.
    """
    if not path.is_file():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return []
    personas_raw = raw.get("personas")
    if not isinstance(personas_raw, dict) or not personas_raw:
        return []

    cfg = load_fleet_config(path)
    pruned: list[str] = []
    kept: dict[str, Any] = {}
    for name, entry in personas_raw.items():
        if isinstance(entry, str):
            spec = PersonaSpec(prompt=entry)
        elif isinstance(entry, dict):
            spec = PersonaSpec(prompt=str(entry.get("prompt") or f"{name}.md"))
        else:
            kept[name] = entry
            continue
        if persona_prompt_resolves(name, spec, cfg):
            kept[name] = entry
        else:
            pruned.append(name)

    if not pruned:
        return []

    raw["personas"] = kept
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return pruned
