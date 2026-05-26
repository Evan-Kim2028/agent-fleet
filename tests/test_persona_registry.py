"""Regression: every declared persona resolves via YamlPersonaResolver."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_fleet.config import FleetConfig, load_fleet_config
from agent_fleet.personas import YamlPersonaResolver, read_persona_body

ROOT = Path(__file__).resolve().parent.parent
AGENTS_PERSONAS = ROOT / "agents" / "personas"
FLEET_EXAMPLE = ROOT / "fleet.example.yaml"
_AGENTS_PERSONAS_DIR = "agents/personas"


def _persona_names_from_dir(personas_dir: Path) -> set[str]:
    if not personas_dir.is_dir():
        return set()
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


def _persona_refs_from_yaml(raw: dict[str, object]) -> set[str]:
    names: set[str] = set()
    personas = raw.get("personas")
    if isinstance(personas, dict):
        names.update(str(name) for name in personas)
    workstreams = raw.get("workstreams")
    if isinstance(workstreams, dict):
        for item in workstreams.get("items") or []:
            if isinstance(item, dict) and item.get("persona"):
                names.add(str(item["persona"]))
    return names


def _yaml_files_pointing_at_agents_personas() -> list[tuple[Path, set[str]]]:
    candidates = sorted(ROOT.rglob("*.yaml"))
    dot_config = ROOT / ".agent-fleet.yaml"
    if dot_config.is_file() and dot_config not in candidates:
        candidates.append(dot_config)
    matches: list[tuple[Path, set[str]]] = []
    for path in candidates:
        if any(part.startswith(".") for part in path.relative_to(ROOT).parts[:-1]):
            continue
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not _yaml_uses_agents_personas(raw):
            continue
        refs = _persona_refs_from_yaml(raw)
        if refs:
            matches.append((path, refs))
    return matches


def _agents_personas_config() -> FleetConfig:
    assert AGENTS_PERSONAS.is_dir(), f"missing repo personas dir: {AGENTS_PERSONAS}"
    return load_fleet_config(FLEET_EXAMPLE, personas_dir=AGENTS_PERSONAS)


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
def agents_personas_config() -> FleetConfig:
    if not AGENTS_PERSONAS.is_dir():
        pytest.skip(f"{AGENTS_PERSONAS} not present in this checkout")
    return _agents_personas_config()


def test_every_agents_personas_file_resolves(agents_personas_config: FleetConfig) -> None:
    resolver = YamlPersonaResolver(agents_personas_config)
    names = _persona_names_from_dir(AGENTS_PERSONAS)
    assert names, f"expected persona files under {AGENTS_PERSONAS}"
    for name in sorted(names):
        _assert_persona_resolves(resolver, name)


def test_fleet_yaml_entries_for_agents_personas_resolve(
    agents_personas_config: FleetConfig,
) -> None:
    resolver = YamlPersonaResolver(agents_personas_config)
    yaml_matches = [
        (path, names) for path, names in _yaml_files_pointing_at_agents_personas() if names
    ]
    if not yaml_matches:
        pytest.skip("no yaml in repo declares personas under personas_dir: agents/personas")
    for _path, persona_names in yaml_matches:
        for name in sorted(persona_names):
            _assert_persona_resolves(resolver, name)


def test_fleet_example_personas_resolve_with_package_dir(fleet_config: FleetConfig) -> None:
    resolver = YamlPersonaResolver(fleet_config)
    for name in sorted(fleet_config.personas):
        _assert_persona_resolves(resolver, name)


def test_repo_persona_overrides_package_fallback(agents_personas_config: FleetConfig) -> None:
    resolver = YamlPersonaResolver(agents_personas_config)
    reviewer = resolver.load("reviewer")
    assert reviewer.prompt_path.resolve() == (AGENTS_PERSONAS / "reviewer.md").resolve()
    coder = resolver.load("coder")
    assert "agent_fleet" in str(coder.prompt_path)
    assert coder.prompt_path.name == "coder.md"


def test_workstream_subcommand_registered() -> None:
    from agent_fleet.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["workstream", "--help"])
    assert exc.value.code == 0
