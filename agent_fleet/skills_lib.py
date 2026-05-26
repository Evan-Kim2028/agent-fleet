"""Load bundled and configured agent skills (SKILL.md)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

_BUNDLED_SKILLS_DIR = Path(__file__).resolve().parent / "skills"
_BASE_KIT_DIR = Path(__file__).resolve().parent / "base-kit"
DEFAULT_QUALITY_REVIEW_SKILL = "thermo-nuclear-code-quality-review"
# Injected on verify_failed when not already in loadout (pstack-first default).
SYSTEMATIC_DEBUGGING_SKILL = "pstack/why"
# Injected when repo pr_loop.enabled (CI fix / watcher workflows).
PR_LOOP_EXECUTE_SKILLS: tuple[str, ...] = (
    "cursor-team-kit/fix-ci",
    "cursor-team-kit/loop-on-ci",
    "cursor-team-kit/get-pr-comments",
)


def base_kit_dir() -> Path:
    return _BASE_KIT_DIR


def base_kit_skill_dirs() -> list[Path]:
    if _BASE_KIT_DIR.is_dir():
        return [_BASE_KIT_DIR]
    return []


def bundled_skill_dirs() -> list[Path]:
    if _BUNDLED_SKILLS_DIR.is_dir():
        return [_BUNDLED_SKILLS_DIR]
    return []


def resolve_skill_path(skill_id: str, skill_dirs: list[Path]) -> Path | None:
    """Resolve a catalog skill id (e.g. cursor-team-kit/deslop) to SKILL.md."""
    for base in skill_dirs:
        if "/" in skill_id:
            slash_candidate = base / skill_id / "SKILL.md"
            if slash_candidate.is_file():
                return slash_candidate
        for candidate in (
            base / skill_id / "SKILL.md",
            base / f"{skill_id}.md",
            base / skill_id / "skill.md",
        ):
            if candidate.is_file():
                return candidate
    return None


def find_skill_path(skill_name: str, skill_dirs: list[Path]) -> Path | None:
    return resolve_skill_path(skill_name, skill_dirs)


def load_skill_text(skill_name: str, skill_dirs: list[Path]) -> str:
    path = resolve_skill_path(skill_name, skill_dirs)
    if path is None:
        raise FileNotFoundError(
            f"Skill not found: {skill_name!r} (searched {len(skill_dirs)} dirs)"
        )
    return path.read_text(encoding="utf-8").strip()


def merge_skill_dirs(*extra: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    merged: list[Path] = []
    for group in extra:
        for path in group:
            resolved = path.expanduser().resolve()
            if resolved not in seen and resolved.is_dir():
                seen.add(resolved)
                merged.append(resolved)
    return merged


def repo_skill_dirs(repo: object) -> list[Path]:
    """Return conventional repo-local skill directories when present."""
    from agent_fleet.repo import RepoConfig

    if not isinstance(repo, RepoConfig):
        return []
    dirs: list[Path] = []
    for name in (".cursor/skills", "skills", ".agents/skills"):
        candidate = (repo.repo_root / name).resolve()
        if candidate.is_dir():
            dirs.append(candidate)
    return dirs


def _loadout_execute_skill_ids(loadout: dict[str, Any]) -> list[str]:
    skill_slots = loadout.get("skill_slots")
    if isinstance(skill_slots, dict):
        return [str(skill_id) for skill_id in (skill_slots.get("execute") or [])]
    skills = loadout.get("skills") or {}
    execute = skills.get("execute") or []
    return [str(skill_id) for skill_id in execute]


def loadout_execute_skill_ids(loadout: dict[str, Any]) -> list[str]:
    return _loadout_execute_skill_ids(loadout)


def loadout_review_skill_ids(loadout: dict[str, Any], pipeline: str = "code_review") -> list[str]:
    skill_slots = loadout.get("skill_slots")
    if isinstance(skill_slots, dict):
        return [str(skill_id) for skill_id in (skill_slots.get("review") or [])]
    pipeline_skills = loadout.get("pipeline_skills") or {}
    phase_skills = pipeline_skills.get(pipeline) or {}
    review = phase_skills.get("review") or []
    return [str(skill_id) for skill_id in review]


def skill_exists_in_base_kit(skill_id: str) -> bool:
    return resolve_skill_path(skill_id, base_kit_skill_dirs()) is not None


def load_loadout(name: str, *, personas_dir: Path | None = None) -> dict[str, Any]:
    from agent_fleet.personas import load_loadout as _load_persona_loadout

    loadout = _load_persona_loadout(name, personas_dir=personas_dir)
    if loadout is None:
        raise FileNotFoundError(f"Loadout not found for persona {name!r}")
    return loadout


def compose_persona_body(
    loadout: dict[str, Any],
    *,
    fleet_overlay: str = "",
    repo_overlay: str = "",
    extra_skills: list[str] | None = None,
    stub_text: str | None = None,
    skill_dirs: list[Path] | None = None,
    level_up_generation: int = 0,
) -> str:
    """Layer base-kit skills, persona stub, fleet/repo overlays into one prompt body."""
    dirs = skill_dirs or base_kit_skill_dirs()
    skill_ids = _loadout_execute_skill_ids(loadout)
    if extra_skills:
        skill_ids.extend(extra_skills)

    parts: list[str] = []
    for skill_id in skill_ids:
        path = resolve_skill_path(skill_id, dirs)
        if path is None:
            continue
        parts.append(path.read_text(encoding="utf-8").strip())

    stub = (stub_text or "").strip()
    if stub:
        parts.append(stub)

    fleet = fleet_overlay.strip()
    if fleet:
        parts.append(f"# Fleet learned\n\n{fleet}")

    repo = repo_overlay.strip()
    if repo:
        gen = f" (generation {level_up_generation})" if level_up_generation > 0 else ""
        parts.append(f"# Repo learned{gen}\n\n{repo}")

    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return "\n\n---\n\n".join(parts).strip()
