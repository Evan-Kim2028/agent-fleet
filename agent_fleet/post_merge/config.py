"""Load per-repo post-merge config from fleet.yaml.

The commands themselves are deliberately not defaulted. A repo's planner and
trigger are that repo's business, and a plausible-looking command that does not
exist on the box is worse than no command at all — it would queue jobs that
silently do nothing. A repo with no ``post_merge`` entry is reported as
unconfigured rather than guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

#: Every key one ``post_merge.repos[]`` entry accepts. An unknown key is an
#: error rather than an ignored typo: a mistyped ``plan_command`` would surface
#: as a batch that never labels or rebuilds anything.
_REPO_KEYS = frozenset(
    {
        "name",
        "path",
        "plan_command",
        "plan_timeout_seconds",
        "trigger_command",
        "trigger_timeout_seconds",
        "handoff_inbox",
        "state_dir",
    }
)

#: Default for ``state_dir``, relative to the agent-fleet home. Plan cache and
#: the dedupe ledger of already-triggered jobs both live here.
DEFAULT_STATE_DIR = "~/.agent-fleet/post-merge"

DEFAULT_PLAN_TIMEOUT = 300
DEFAULT_TRIGGER_TIMEOUT = 3600


def expand_path(value: str) -> str:
    """*value* with ``~`` expanded, or ``""`` when it is unset.

    The documented config form is ``path: ~/code/lake-of-rage`` and neither
    ``gh`` nor ``subprocess`` expands a tilde: the raw string is handed to the
    spawn as a working directory and fails with FileNotFoundError. Every
    working directory in this package goes through here.
    """
    return str(Path(value).expanduser()) if value else ""


@dataclass(frozen=True)
class RepoSpec:
    """One repo's post-merge wiring, loaded from ``post_merge.repos[]``."""

    name: str
    path: str = ""
    #: Reads changed file paths on stdin, prints a plan as JSON on stdout.
    plan_command: str = ""
    plan_timeout_seconds: int = DEFAULT_PLAN_TIMEOUT
    #: Run once per deduplicated job. Supports ``{job}`` and ``{slot}``.
    trigger_command: str = ""
    trigger_timeout_seconds: int = DEFAULT_TRIGGER_TIMEOUT
    #: Directory the hand-off notes and INDEX are written to.
    handoff_inbox: str = ""
    #: Where the plan cache and the triggered-jobs ledger live.
    state_dir: str = DEFAULT_STATE_DIR

    @property
    def is_configured(self) -> bool:
        """True when this repo can actually do something."""
        return bool(self.plan_command)

    def cache_dir(self) -> Path:
        return Path(self.state_dir).expanduser() / "plans" / self.name

    def ledger_path(self) -> Path:
        return Path(self.state_dir).expanduser() / "triggered" / f"{self.name}.txt"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "plan_command": self.plan_command,
            "plan_timeout_seconds": self.plan_timeout_seconds,
            "trigger_command": self.trigger_command,
            "trigger_timeout_seconds": self.trigger_timeout_seconds,
            "handoff_inbox": self.handoff_inbox,
            "state_dir": self.state_dir,
        }


def _read_post_merge_block(fleet_config_path: Path | None) -> dict[str, Any]:
    """The raw ``post_merge:`` mapping from fleet.yaml, or ``{}``.

    A missing or unreadable config yields an empty block rather than raising, so
    a box with no config gets a plain "not configured" report.
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
    block = data.get("post_merge")
    return block if isinstance(block, dict) else {}


def _int(block: dict[str, Any], key: str, default: int) -> int:
    value = block.get(key)
    if value is None:
        return default
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"post_merge.repos[].{key} must be an integer, got {value!r}") from exc


def parse_repo_spec(raw: dict[str, Any]) -> RepoSpec:
    """Build a RepoSpec from one ``post_merge.repos[]`` entry."""
    unknown = set(raw) - _REPO_KEYS
    if unknown:
        raise ValueError(
            f"post_merge.repos[] contains unknown key(s) {sorted(unknown)}; "
            f"valid keys: {sorted(_REPO_KEYS)}"
        )
    return RepoSpec(
        name=str(raw.get("name") or ""),
        path=str(raw.get("path") or ""),
        plan_command=str(raw.get("plan_command") or ""),
        plan_timeout_seconds=_int(raw, "plan_timeout_seconds", DEFAULT_PLAN_TIMEOUT),
        trigger_command=str(raw.get("trigger_command") or ""),
        trigger_timeout_seconds=_int(raw, "trigger_timeout_seconds", DEFAULT_TRIGGER_TIMEOUT),
        handoff_inbox=str(raw.get("handoff_inbox") or ""),
        state_dir=str(raw.get("state_dir") or DEFAULT_STATE_DIR),
    )


def load_repo_specs(fleet_config_path: Path | None = None) -> dict[str, RepoSpec]:
    """Read ``post_merge.repos[]`` from fleet.yaml into a ``{repo: RepoSpec}`` map.

    A missing or unreadable config yields an empty map, which the CLI reports
    plainly rather than fabricating a planner.
    """
    entries = _read_post_merge_block(fleet_config_path).get("repos")
    if not isinstance(entries, list):
        return {}
    specs: dict[str, RepoSpec] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        spec = parse_repo_spec(entry)
        specs[spec.name] = spec
    return specs


def resolve_repo_spec(
    repo: str,
    *,
    repo_path: str = "",
    fleet_config_path: Path | None = None,
) -> RepoSpec:
    """The spec for *repo*, optionally overriding its checkout path.

    An unconfigured repo still comes back as a spec — one that reports itself
    unconfigured — so the CLI has a single shape to render.
    """
    specs = load_repo_specs(fleet_config_path)
    spec = specs.get(repo)
    if spec is None:
        return RepoSpec(name=repo, path=repo_path)
    if repo_path:
        return replace(spec, path=repo_path)
    return spec
