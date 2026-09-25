"""Turn a PR's changed-file list into the facts batching cares about.

Three things are derived, all purely from file paths so the profile is cheap
and testable without a checkout:

* the **deploy unit** — which deploy one verify step would cover,
* the **dbt models** touched, expanded downstream via the compiled manifest,
* **risk flags** — migrations, deploy scripts, prod-write tools.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from agent_fleet.merge_plan.types import ApprovedPR, ChangeProfile, RepoSpec

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

# ---------------------------------------------------------------------------
# Per-repo deploy units. Longest matching prefix wins; a PR touching two units
# is assigned the higher-precedence unit (see _UNIT_PRECEDENCE).
# ---------------------------------------------------------------------------

LAKE_OF_RAGE_UNITS: dict[str, str] = {
    "api/": "lor-api",
    "orchestration/": "orchestration",
    "transform/models/": "dbt",
    "transform/macros/": "dbt",
    "transform/": "dbt",
    "pipelines/": "pipelines",
    "packages/": "pipelines",
    "infra/vps/": "lor-api",
}

SILPH_UNITS: dict[str, str] = {
    "api/": "api",
    "frontend/": "frontend",
    "mobile/": "mobile",
    "pipeline/": "pipeline",
}

AGENT_FLEET_UNITS: dict[str, str] = {
    "agent_fleet/": "package",
    "tests/": "package",
    "docs/": "package",
}

#: When a PR spans several deploy units, the earliest entry here wins. Deploy
#: is the heaviest step, so a change that also rebuilds the API is shipped as
#: an API batch rather than a cheaper-looking one.
_UNIT_PRECEDENCE: tuple[str, ...] = (
    "lor-api",
    "api",
    "migration",
    "orchestration",
    "dbt",
    "pipelines",
    "frontend",
    "mobile",
    "pipeline",
    "package",
)

#: Path fragments that mark a migration / schema change.
_MIGRATION_MARKERS: tuple[str, ...] = (
    "alembic",
    "migration",
    "migrations/",
    "gold_catalog.json",
    "packages/lakestore",
    "schema.sql",
)

#: Path fragments that mark a deploy or workflow change.
_DEPLOY_MARKERS: tuple[str, ...] = (
    ".github/workflows/",
    "infra/vps/",
    "deploy",
    "Dockerfile",
    "docker-compose",
    "systemd/",
    "infra/terraform",
)

#: Path fragments that mark a tool that writes to production.
_PROD_WRITE_MARKERS: tuple[str, ...] = (
    "pipe/ops/",
    "pipelines/",
    "ops/",
    "scripts/",
)

#: dbt model source extensions.
_DBT_MODEL_EXTS: tuple[str, ...] = (".sql", ".py", ".yml", ".yaml")
_DBT_MODELS_PREFIX = "transform/models/"


def _matches_any(path: str, markers: Iterable[str]) -> bool:
    return any(marker in path for marker in markers)


def deploy_unit_for(paths: Iterable[str], units: Mapping[str, str]) -> str:
    """Return the single deploy unit covering *paths*.

    Returns "" when the repo has no unit table for these paths, so the caller
    can flag ``unknown_repo`` rather than guessing a unit.
    """
    found: set[str] = set()
    for path in paths:
        best_prefix = ""
        best_unit = ""
        for prefix, unit in units.items():
            if path.startswith(prefix) and len(prefix) > len(best_prefix):
                best_prefix, best_unit = prefix, unit
        if best_unit:
            found.add(best_unit)
    if not found:
        return ""
    for unit in _UNIT_PRECEDENCE:
        if unit in found:
            return unit
    # No unit in the precedence table (a custom unit): pick deterministically.
    return sorted(found)[0]


def dbt_models_for(paths: Iterable[str]) -> tuple[str, ...]:
    """Model names directly edited by *paths*, sorted for determinism.

    ``transform/models/gold/sales.sql`` -> ``gold.sales``; a dbt project may
    configure a different separator, so the name keeps its directory path
    joined by ``.``.
    """
    models: set[str] = set()
    for path in paths:
        if not path.startswith(_DBT_MODELS_PREFIX):
            continue
        if not path.endswith(_DBT_MODEL_EXTS):
            continue
        relative = path[len(_DBT_MODELS_PREFIX) :]
        stem = relative.rsplit(".", 1)[0]
        parts = [p for p in stem.split("/") if p]
        if parts:
            models.add(".".join(parts))
    return tuple(sorted(models))


def risk_flags_for(paths: Iterable[str], extra: Iterable[str] = ()) -> tuple[str, ...]:
    """Risk flags raised by *paths*: migration / deploy / prod_write."""
    paths = list(paths)
    flags: set[str] = set()
    if any(_matches_any(p, _MIGRATION_MARKERS) for p in paths):
        flags.add("migration")
    if any(_matches_any(p, _DEPLOY_MARKERS) for p in paths):
        flags.add("deploy")
    if any(_matches_any(p, _PROD_WRITE_MARKERS) for p in paths):
        flags.add("prod_write")
    for glob in extra:
        if any(Path(p).match(glob) for p in paths):
            flags.add("migration")
    return tuple(f for f in ("migration", "deploy", "prod_write") if f in flags)


def load_manifest_parent_map(manifest_path: Path) -> dict[str, list[str]]:
    """Return the dbt manifest ``parent_map``, or {} when unavailable.

    A missing or unreadable manifest is not an error: the caller falls back to
    direct models only.
    """
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError, TypeError:
        return {}
    if not isinstance(data, dict):
        return {}
    parent_map = data.get("parent_map")
    if not isinstance(parent_map, dict):
        return {}
    return {str(k): [str(c) for c in v] for k, v in parent_map.items() if isinstance(v, list)}


def expand_downstream(
    models: Iterable[str],
    parent_map: Mapping[str, list[str]],
    *,
    package: str = "transform",
) -> tuple[str, ...]:
    """Expand *models* to every model downstream of them.

    Walks the manifest's ``parent_map`` transitively, keeping only model
    nodes.  A model that nothing depends on maps to itself, so the result is
    always a superset of the input.
    """
    seeds = {f"model.{package}.{m}" for m in models}
    if not seeds or not parent_map:
        return tuple(sorted(models))
    seen: set[str] = set(seeds)
    queue = list(seeds)
    while queue:
        node = queue.pop()
        for child in parent_map.get(node, ()):
            if child in seen or not child.startswith("model."):
                continue
            seen.add(child)
            queue.append(child)
    suffix = f".{package}."
    expanded = {n.split(suffix, 1)[1] for n in seen if suffix in n}
    return tuple(sorted(expanded or models))


def build_profile(
    pr: ApprovedPR,
    *,
    files: Iterable[str],
    base_ref: str = "",
    lines_changed: int = 0,
    repo_spec: RepoSpec | None = None,
    parent_map: Mapping[str, list[str]] | None = None,
) -> ChangeProfile:
    """Build the change profile for one approved, non-stale PR.

    When *parent_map* is supplied the dbt ``--select`` set is the downstream
    closure (so the rebuild covers everything the change can invalidate);
    otherwise only the directly edited models are selected.
    """
    file_list = tuple(sorted({f for f in files if f}))
    units = repo_spec.deploy_units if repo_spec else {}
    models = dbt_models_for(file_list)
    if parent_map:
        select = expand_downstream(models, parent_map)
        select_source = "manifest"
    else:
        select = models
        select_source = "direct"
    extra_risk = repo_spec.risk_globs if repo_spec else ()
    return ChangeProfile(
        repo=pr.repo,
        pr_number=pr.pr_number,
        base_ref=base_ref,
        files=file_list,
        deploy_unit=deploy_unit_for(file_list, units),
        dbt_models=models,
        dbt_select=select,
        dbt_select_source=select_source if models else "",
        risk_flags=risk_flags_for(file_list, extra_risk),
        lines_changed=lines_changed,
        stale=False,
    )
