"""Regression: every declared persona resolves via YamlPersonaResolver."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from agent_fleet.config import FleetConfig, load_fleet_config
from agent_fleet.personas import YamlPersonaResolver, read_persona_body

ROOT = Path(__file__).resolve().parent.parent
FLEET_EXAMPLE = ROOT / "fleet.example.yaml"
_AGENTS_PERSONAS_DIR = "agents/personas"


def _git_common_root() -> Path | None:
    result = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    git_common = Path(result.stdout.strip())
    if not git_common.is_absolute():
        git_common = (ROOT / git_common).resolve()
    if git_common.name == ".git":
        return git_common.parent
    return git_common.parent.parent


def _agents_personas_dir() -> Path | None:
    for base in (ROOT, _git_common_root()):
        if base is None:
            continue
        candidate = base / "agents" / "personas"
        if candidate.is_dir():
            return candidate
    return None


def _persona_names_from_dir(personas_dir: Path) -> set[str]:
    names: set[str] = set()
    for path in personas_dir.glob("*.md"):
        names.add(path.stem)
    for path in personas_dir.glob("*.loadout.yaml"):
        names.add(path.stem.replace(".loadout", ""))
    return names


def _yaml_uses_agents_personas(raw: dict[str, object]) -> bool:
    personas_dir = raw.get("personas_dir")
    if personas_dir is None:
        return False
    normalized = str(personas_dir).rstrip("/")
    return normalized == _AGENTS_PERSONAS_DIR


def _persona_refs_from_yaml(raw: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    personas = raw.get("personas")
    if isinstance(personas, dict):
        names.update(str(name) for name in personas)
    workstreams: Any = raw.get("workstreams")
    if isinstance(workstreams, dict):
        items: Any = workstreams.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("persona"):
                    names.add(str(item["persona"]))
    return names


def _yaml_files_pointing_at_agents_personas() -> list[tuple[Path, set[str]]]:
    search_roots = [ROOT]
    main_root = _git_common_root()
    if main_root is not None and main_root not in search_roots:
        search_roots.append(main_root)

    matches: list[tuple[Path, set[str]]] = []
    seen_paths: set[Path] = set()
    for base in search_roots:
        candidates = sorted(base.rglob("*.yaml"))
        dot_config = base / ".agent-fleet.yaml"
        if dot_config.is_file() and dot_config not in candidates:
            candidates.append(dot_config)
        for path in candidates:
            if "node_modules" in path.parts or ".worktrees" in path.parts:
                continue
            resolved = path.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            except yaml.YAMLError:
                continue
            if not isinstance(raw, dict) or not _yaml_uses_agents_personas(raw):
                continue
            refs = _persona_refs_from_yaml(raw)
            if refs:
                matches.append((path, refs))
    return matches


def _agents_personas_config(personas_dir: Path) -> FleetConfig:
    return load_fleet_config(FLEET_EXAMPLE, personas_dir=personas_dir)


@pytest.fixture
def fleet_config() -> FleetConfig:
    return load_fleet_config(FLEET_EXAMPLE)


def _assert_persona_resolves(resolver: YamlPersonaResolver, name: str) -> None:
    persona = resolver.load(name)
    if persona.body:
        assert persona.body.strip()
    else:
        assert persona.prompt_path.is_file(), f"{name}: prompt missing at {persona.prompt_path}"
        assert read_persona_body(persona).strip()


@pytest.fixture(scope="module")
def agents_personas_dir() -> Path:
    personas_dir = _agents_personas_dir()
    if personas_dir is None:
        pytest.skip("agents/personas not present in this checkout")
    return personas_dir


@pytest.fixture(scope="module")
def agents_personas_config(agents_personas_dir: Path) -> FleetConfig:
    return _agents_personas_config(agents_personas_dir)


def test_every_agents_personas_file_resolves(
    agents_personas_dir: Path,
    agents_personas_config: FleetConfig,
) -> None:
    resolver = YamlPersonaResolver(agents_personas_config)
    names = _persona_names_from_dir(agents_personas_dir)
    assert names, f"expected persona files under {agents_personas_dir}"
    for name in sorted(names):
        _assert_persona_resolves(resolver, name)


def test_fleet_yaml_entries_for_agents_personas_resolve(
    agents_personas_config: FleetConfig,
) -> None:
    resolver = YamlPersonaResolver(agents_personas_config)
    yaml_matches = _yaml_files_pointing_at_agents_personas()
    if not yaml_matches:
        pytest.skip("no yaml in repo declares personas under personas_dir: agents/personas")
    for _path, persona_names in yaml_matches:
        for name in sorted(persona_names):
            _assert_persona_resolves(resolver, name)


def test_fleet_example_personas_resolve_with_package_dir(fleet_config: FleetConfig) -> None:
    resolver = YamlPersonaResolver(fleet_config)
    for name in sorted(fleet_config.personas):
        _assert_persona_resolves(resolver, name)


def test_repo_persona_overrides_package_fallback(
    agents_personas_dir: Path,
    agents_personas_config: FleetConfig,
) -> None:
    resolver = YamlPersonaResolver(agents_personas_config)
    reviewer = resolver.load("reviewer")
    assert reviewer.prompt_path.resolve() == (agents_personas_dir / "reviewer.md").resolve()
    coder = resolver.load("coder")
    assert "agent_fleet" in str(coder.prompt_path)
    assert coder.prompt_path.name == "coder.md"
