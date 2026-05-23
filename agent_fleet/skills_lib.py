"""Load bundled and configured agent skills (SKILL.md)."""

from __future__ import annotations

from pathlib import Path

_BUNDLED_SKILLS_DIR = Path(__file__).resolve().parent / "skills"
DEFAULT_QUALITY_REVIEW_SKILL = "thermo-nuclear-code-quality-review"


def bundled_skill_dirs() -> list[Path]:
    if _BUNDLED_SKILLS_DIR.is_dir():
        return [_BUNDLED_SKILLS_DIR]
    return []


def find_skill_path(skill_name: str, skill_dirs: list[Path]) -> Path | None:
    for base in skill_dirs:
        for candidate in (
            base / skill_name / "SKILL.md",
            base / f"{skill_name}.md",
            base / skill_name / "skill.md",
        ):
            if candidate.is_file():
                return candidate
    return None


def load_skill_text(skill_name: str, skill_dirs: list[Path]) -> str:
    path = find_skill_path(skill_name, skill_dirs)
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
