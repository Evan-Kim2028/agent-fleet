"""Load per-repo batching config from fleet.yaml.

The executor templates are deliberately **not** defaulted.  The scripts the
two operator sessions actually merge with differ per repo and take different
argument shapes, and hardcoding a plausible-looking command that does not
exist on the box would be worse than saying nothing.  When a repo has no
template configured, ``merge-plan`` reports that plainly.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import yaml

from agent_fleet.merge_plan.profile import (
    AGENT_FLEET_UNITS,
    LAKE_OF_RAGE_UNITS,
    SILPH_UNITS,
)
from agent_fleet.merge_plan.types import ClusterHold, ExecutorSpec, RepoSpec

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Every key ``merge_plan.executor`` accepts. An unknown key is an error, not a
#: silently ignored typo: a mistyped command template would surface as a merge
#: that mysteriously does nothing, which is the expensive kind of failure.
_EXECUTOR_KEYS = frozenset(
    {
        "holds",
        "exclusive_groups",
        "post_merge_hold_seconds",
        "rebase_command",
        "command_timeout_seconds",
        "conflict_exit_code",
        "state_dir",
    }
)

#: Keys accepted inside one ``executor.holds[]`` entry.
_HOLD_KEYS = frozenset({"name", "match"})

#: Keys accepted inside one ``executor.holds[].match`` block.
_HOLD_MATCH_KEYS = frozenset({"lanes", "deploy_units"})

#: Default for ``state_dir``, relative to the agent-fleet home.
DEFAULT_STATE_DIR = "~/.agent-fleet/merge"

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
    if raw.get("deploy_template"):
        spec.deploy_template = str(raw["deploy_template"])
    if raw.get("verify_template"):
        spec.verify_template = str(raw["verify_template"])
    if raw.get("rebase_template"):
        spec.rebase_template = str(raw["rebase_template"])
    if raw.get("dbt_manifest_path"):
        spec.dbt_manifest_path = str(raw["dbt_manifest_path"])
    globs = raw.get("risk_globs")
    if isinstance(globs, list):
        spec.risk_globs = tuple(str(g) for g in globs)
    return spec


def _read_merge_plan_block(fleet_config_path: Path | None) -> dict[str, Any]:
    """The raw ``merge_plan:`` mapping from fleet.yaml, or ``{}``.

    A missing or unreadable config yields an empty block rather than raising,
    so a box with no config still gets built-in deploy units.
    """
    from agent_fleet.fleet_paths import default_fleet_config_path

    path = Path(fleet_config_path) if fleet_config_path else default_fleet_config_path()
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError, yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    block = data.get("merge_plan")
    return block if isinstance(block, dict) else {}


def load_merge_plan_config(fleet_config_path: Path | None = None) -> dict[str, RepoSpec]:
    """Read ``merge_plan:`` from fleet.yaml into a ``{repo: RepoSpec}`` map.

    A missing or unreadable config yields an empty map: the CLI then relies on
    built-in deploy units and reports that no executor template is configured.
    """
    block = _read_merge_plan_block(fleet_config_path)
    entries = block.get("repos")
    if not isinstance(entries, list):
        return {}
    specs: dict[str, RepoSpec] = {}
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name"):
            spec = parse_repo_spec(entry)
            specs[spec.name] = spec
    return specs


def _parse_hold(raw: object) -> ClusterHold | None:
    """One ``executor.holds[]`` entry, or ``None`` when it has no name."""
    if not isinstance(raw, dict):
        return None
    entry = cast("Mapping[str, object]", raw)
    unknown = set(entry) - _HOLD_KEYS
    if unknown:
        raise ValueError(
            f"merge_plan.executor.holds[] contains unknown key(s) {sorted(unknown)}; "
            f"valid keys: {sorted(_HOLD_KEYS)}"
        )
    name = str(entry.get("name") or "")
    if not name:
        return None
    match = entry.get("match")
    if match is not None and not isinstance(match, dict):
        raise ValueError(f"merge_plan.executor.holds[{name!r}].match must be a mapping")
    patterns = cast("Mapping[str, object]", match) if isinstance(match, dict) else {}
    unknown = set(patterns) - _HOLD_MATCH_KEYS
    if unknown:
        raise ValueError(
            f"merge_plan.executor.holds[{name!r}].match contains unknown key(s) "
            f"{sorted(unknown)}; valid keys: {sorted(_HOLD_MATCH_KEYS)}"
        )
    lanes = patterns.get("lanes")
    units = patterns.get("deploy_units")
    return ClusterHold(
        name=name,
        lanes=tuple(str(v) for v in lanes) if isinstance(lanes, list) else (),
        deploy_units=tuple(str(v) for v in units) if isinstance(units, list) else (),
    )


def parse_executor_spec(raw: Mapping[str, object] | None) -> ExecutorSpec:
    """Build an ExecutorSpec from the ``merge_plan.executor:`` mapping.

    Unlike the repo entries, which stay permissive for forward compatibility,
    this block is validated strictly: every key is checked, so a typo surfaces
    at load time instead of as a merge that quietly never happens.
    """
    if not isinstance(raw, dict):
        return ExecutorSpec()
    block = cast("Mapping[str, object]", raw)
    unknown = set(block) - _EXECUTOR_KEYS
    if unknown:
        raise ValueError(
            f"merge_plan.executor contains unknown key(s) {sorted(unknown)}; "
            f"valid keys: {sorted(_EXECUTOR_KEYS)}"
        )

    holds: list[ClusterHold] = []
    raw_holds = block.get("holds")
    if raw_holds is not None and not isinstance(raw_holds, list):
        raise ValueError("merge_plan.executor.holds must be a list")
    for entry in raw_holds or []:
        hold = _parse_hold(entry)
        if hold is not None:
            holds.append(hold)

    groups: list[tuple[str, ...]] = []
    raw_groups = block.get("exclusive_groups")
    if raw_groups is not None and not isinstance(raw_groups, list):
        raise ValueError("merge_plan.executor.exclusive_groups must be a list of lists")
    for entry in raw_groups or []:
        if not isinstance(entry, list) or not entry:
            raise ValueError(
                "merge_plan.executor.exclusive_groups entries must be non-empty lists of repo names"
            )
        groups.append(tuple(str(v) for v in entry))

    def _int(key: str, default: int) -> int:
        value = block.get(key)
        if value is None:
            return default
        try:
            return int(str(value))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"merge_plan.executor.{key} must be an integer, got {value!r}"
            ) from exc

    return ExecutorSpec(
        holds=tuple(holds),
        exclusive_groups=tuple(groups),
        post_merge_hold_seconds=_int("post_merge_hold_seconds", 0),
        rebase_command=str(block.get("rebase_command") or ""),
        command_timeout_seconds=_int("command_timeout_seconds", 1800),
        conflict_exit_code=_int("conflict_exit_code", 3),
        state_dir=str(block.get("state_dir") or DEFAULT_STATE_DIR),
    )


def load_executor_spec(fleet_config_path: Path | None = None) -> ExecutorSpec:
    """Read ``merge_plan.executor:`` from fleet.yaml.

    Returns inert defaults when the block is absent. Raises ``ValueError`` on a
    malformed block, which the CLI surfaces rather than silently degrading.
    """
    return parse_executor_spec(_read_merge_plan_block(fleet_config_path).get("executor"))


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
    except OSError, subprocess.SubprocessError:
        pass
    return path.name
