"""PR review configuration from repo .agent-fleet.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_fleet.skills_lib import DEFAULT_QUALITY_REVIEW_SKILL

DEFAULT_TRIVIAL_PATTERNS = (
    r"\.md$",
    r"\.txt$",
    r"^docs/",
    r"^README",
    r"^LICENSE",
    r"^CHANGELOG",
    r"^\.gitignore$",
    r"^\.editorconfig$",
    r"package-lock\.json$",
    r"yarn\.lock$",
    r"poetry\.lock$",
    r"Cargo\.lock$",
    r"Pipfile\.lock$",
    r"uv\.lock$",
    r"\.(png|jpe?g|svg|gif|ico|woff2?|ttf|eot)$",
)

DEFAULT_AREA_PREFIXES: dict[str, tuple[str, ...]] = {
    "frontend": ("frontend/", "web/", "apps/web/"),
    "backend": ("api/", "packages/", "pipeline/", "pipelines/", "src/"),
}


@dataclass(frozen=True)
class PrReviewConfig:
    enabled: bool = True
    use_in_code_review: bool = True
    overlay_path: Path | None = None
    reviewer_persona: str = "pr-analyzer"
    comment_title: str = "Composer PR Analysis"
    backend_label: str = "Agent Fleet"
    trivial_patterns: tuple[str, ...] = DEFAULT_TRIVIAL_PATTERNS
    oversized_threshold: int = 50
    area_prefixes: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(DEFAULT_AREA_PREFIXES)
    )
    passes: tuple[str, ...] = ("backend-security", "frontend")
    max_diff_chars: int = 8000
    fanout_threshold: int = 20
    log_dir: Path | None = None
    quality_review_enabled: bool = True
    quality_review_skill: str = DEFAULT_QUALITY_REVIEW_SKILL


def _tuple_paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value)


def load_pr_review_config(repo_root: Path, raw: dict[str, Any] | None) -> PrReviewConfig | None:
    """Load pr_review section from parsed .agent-fleet.yaml dict."""
    section = (raw or {}).get("pr_review")
    if section is None or section is False:
        return None
    if not isinstance(section, dict):
        return PrReviewConfig()

    overlay_raw = section.get("overlay")
    overlay_path = None
    if overlay_raw:
        overlay_path = (repo_root / str(overlay_raw)).resolve()

    area_prefixes = dict(DEFAULT_AREA_PREFIXES)
    raw_areas = section.get("area_prefixes") or {}
    if isinstance(raw_areas, dict):
        for key, paths in raw_areas.items():
            parsed = _tuple_paths(paths)
            if parsed:
                area_prefixes[str(key)] = parsed

    passes_raw = section.get("passes")
    passes: tuple[str, ...]
    if isinstance(passes_raw, list) and passes_raw:
        passes = tuple(str(p) for p in passes_raw)
    else:
        passes = ("backend-security", "frontend")

    log_dir_raw = section.get("log_dir")
    log_dir = Path(str(log_dir_raw)).expanduser() if log_dir_raw else None

    quality_raw = section.get("quality_review")
    quality_enabled = True
    quality_skill = DEFAULT_QUALITY_REVIEW_SKILL
    if quality_raw is False:
        quality_enabled = False
    elif isinstance(quality_raw, dict):
        quality_enabled = bool(quality_raw.get("enabled", True))
        quality_skill = str(quality_raw.get("skill") or DEFAULT_QUALITY_REVIEW_SKILL)

    return PrReviewConfig(
        enabled=bool(section.get("enabled", True)),
        use_in_code_review=bool(section.get("use_in_code_review", True)),
        overlay_path=overlay_path,
        reviewer_persona=str(section.get("reviewer_persona") or "pr-analyzer"),
        comment_title=str(section.get("comment_title") or "Composer PR Analysis"),
        backend_label=str(section.get("backend_label") or "Agent Fleet"),
        trivial_patterns=_tuple_paths(section.get("trivial_patterns")) or DEFAULT_TRIVIAL_PATTERNS,
        oversized_threshold=int(section.get("oversized_threshold") or 50),
        area_prefixes=area_prefixes,
        passes=passes,
        max_diff_chars=int(section.get("max_diff_chars") or 8000),
        fanout_threshold=int(section.get("fanout_threshold") or 20),
        log_dir=log_dir,
        quality_review_enabled=quality_enabled,
        quality_review_skill=quality_skill,
    )


def load_overlay_text(config: PrReviewConfig) -> str:
    if config.overlay_path and config.overlay_path.is_file():
        return config.overlay_path.read_text(encoding="utf-8").strip()
    return ""
