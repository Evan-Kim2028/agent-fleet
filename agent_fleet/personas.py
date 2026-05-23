"""Persona resolution from markdown prompts, YAML registry, and Hermes skills."""

from __future__ import annotations

from pathlib import Path

from agent_fleet.config import FleetConfig, PersonaSpec
from agent_fleet.hooks import Persona
from agent_fleet.skills_lib import find_skill_path

_DEFAULT_ALLOWED_TOOLS = ["read_file", "write_file", "run_command"]


def _resolve_prompt_path(spec: PersonaSpec, cfg: FleetConfig) -> Path:
    prompt = spec.prompt.strip()
    if prompt.startswith(("/", "~")):
        return Path(prompt).expanduser().resolve()
    direct = Path(prompt)
    if direct.is_absolute() and direct.exists():
        return direct
    if direct.exists():
        return direct.resolve()
    in_personas = cfg.personas_dir / prompt
    if in_personas.exists():
        return in_personas.resolve()
    if not prompt.endswith(".md"):
        with_suffix = cfg.personas_dir / f"{prompt}.md"
        if with_suffix.exists():
            return with_suffix.resolve()
    if spec.skill:
        skill_path = find_skill_path(spec.skill, cfg.skill_dirs)
        if skill_path:
            return skill_path
    raise ValueError(
        f"Persona prompt not found: {prompt!r} "
        f"(searched personas_dir={cfg.personas_dir}, skill={spec.skill!r})"
    )


class YamlPersonaResolver:
    """Load personas from fleet.yaml + markdown/skill files."""

    def __init__(self, config: FleetConfig) -> None:
        self._config = config

    def list_personas(self) -> list[str]:
        names = set(self._config.personas.keys())
        if self._config.personas_dir.exists():
            for path in sorted(self._config.personas_dir.glob("*.md")):
                names.add(path.stem)
        return sorted(names)

    def load(self, name: str) -> Persona:
        spec = self._config.personas.get(name)
        if spec is None:
            spec = PersonaSpec(prompt=f"{name}.md")
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
            allowed_tools=allowed_tools,
            capabilities=capabilities,
            allowed_paths=allowed_paths,
            model=spec.model or self._config.default_model,
            mode=spec.mode or self._config.default_mode,
            extra_instructions=spec.extra_instructions,
        )
