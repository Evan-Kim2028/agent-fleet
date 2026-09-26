"""Load per-repo batching config from fleet.yaml.

The executor templates are deliberately **not** defaulted.  The scripts the
two operator sessions actually merge with differ per repo and take different
argument shapes, and hardcoding a plausible-looking command that does not
exist on the box would be worse than saying nothing.  When a repo has no
template configured, ``merge-plan`` reports that plainly.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from agent_fleet.merge_plan.profile import (
    AGENT_FLEET_UNITS,
    LAKE_OF_RAGE_UNITS,
    SILPH_UNITS,
)
from agent_fleet.merge_plan.types import RepoSpec

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Built-in deploy-unit tables, keyed by repo name. A repo absent here gets
#: no units, and its PRs are reported rather than assigned a guessed unit.
BUILTIN_UNITS: dict[str, dict[str, str]] = {
    "lake-of-rage": dict(LAKE_OF_RAGE_UNITS),
    "silphcoanalytics": dict(SILPH_UNITS),
    "agent-fleet": dict(AGENT_FLEET_UNITS),
}

BUILTIN_DBT_MANIFEST = "transform/target/manifest.json"


def _normalize_repo_name(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def builtin_spec(repo: str, path: str = "") -> RepoSpec:
    """A RepoSpec carrying the built-in deploy units for *repo*."""
    key = _normalize_repo_name(repo)
    return RepoSpec(
        name=repo,
        path=path,
        deploy_units=dict(BUILTIN_UNITS.get(key, {})),
        dbt_manifest_path=BUILTIN_DBT_MANIFEST,
    )


def parse_repo_spec(raw: Mapping[str, Any]) -> RepoSpec:
    """Build a RepoSpec from one ``merge_plan.repos[]`` entry."""
    name = str(raw.get("name") or "")
    spec = builtin_spec(name, str(raw.get("path") or ""))
    if raw.get("path"):
        spec.path = str(raw["path"])
    units = raw.get("deploy_units")
    if isinstance(units, dict):
        spec.deploy_units = {str(k): str(v) for k, v in units.items()}
    if raw.get("merge_template"):
        spec.merge_template = str(raw["merge_template"])
    if raw.get("merge_per_pr_template"):
        spec.merge_per_pr_template = str(raw["merge_per_pr_template"])
    if raw.get("dbt_manifest_path"):
        spec.dbt_manifest_path = str(raw["dbt_manifest_path"])
    globs = raw.get("risk_globs")
    if isinstance(globs, list):
        spec.risk_globs = tuple(str(g) for g in globs)
    return spec


def load_merge_plan_config(fleet_config_path: Path | None = None) -> dict[str, RepoSpec]:
    """Read ``merge_plan:`` from fleet.yaml into a ``{repo: RepoSpec}`` map.

    A missing or unreadable config yields an empty map: the CLI then relies on
    built-in deploy units and reports that no executor template is configured.
    """
    from agent_fleet.fleet_paths import default_fleet_config_path

    path = Path(fleet_config_path) if fleet_config_path else default_fleet_config_path()
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(data, dict):
        return {}
    block = data.get("merge_plan")
    if not isinstance(block, dict):
        return {}
    entries = block.get("repos")
    if not isinstance(entries, list):
        return {}
    specs: dict[str, RepoSpec] = {}
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name"):
            spec = parse_repo_spec(entry)
            specs[spec.name] = spec
    return specs


def resolve_repo_specs(
    repo_paths: list[str],
    *,
    fleet_config_path: Path | None = None,
) -> dict[str, RepoSpec]:
    """Merge ``--repo-path`` arguments over the fleet.yaml repo entries.

    An explicitly given path always wins for its repo, so a one-off run
    against a worktree does not need a config edit.
    """
    specs = load_merge_plan_config(fleet_config_path)
    for raw_path in repo_paths:
        path = Path(raw_path).expanduser()
        name = _repo_name_from_path(path)
        if name in specs:
            specs[name].path = str(path)
        else:
            specs[name] = builtin_spec(name, str(path))
    return specs


def _repo_name_from_path(path: Path) -> str:
    """Best-effort repo name from a checkout path.

    Uses the ``origin`` remote when readable (that is the real repo name),
    falling back to the directory name so a synthetic fixture still works.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
            cwd=str(path),
        )
        if result.returncode == 0 and result.stdout.strip():
            url = result.stdout.strip().removesuffix(".git")
            return url.rsplit("/", 1)[-1]
    except (OSError, subprocess.SubprocessError):
        pass
    return path.name
