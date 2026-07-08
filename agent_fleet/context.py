"""Shared fleet context — load-bearing seam between CLI args and domain commands.

``build_fleet_context`` is the single adapter that collapses:
- workspace resolution (arg → cwd)
- config loading (always calls load_fleet_config, no redundant if-guard)
- repo discovery (find_repo_config vs. resolve_repo_config based on use_env_target_config)
- persona fallback chain (arg → repo default → fleet config default)
- optional personas_dir override from repo
- optional require_env check (backend API key guard)

Commands that touch a backend always pass ``require_env=True``; dry-run or
read-only commands pass the default ``require_env=False``.

The issue-dispatch path opts into ``use_env_target_config=True`` so that the
``AGENT_FLEET_TARGET_CONFIG`` env-var protocol is honoured; all other commands
use ``find_repo_config`` semantics (``use_env_target_config=False``, the default).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_fleet.config import FleetConfig
    from agent_fleet.repo import RepoConfig


@dataclass
class FleetContext:
    """Resolved, ready-to-use context for a single fleet command invocation."""

    workspace: Path
    config: FleetConfig
    repo: RepoConfig | None
    persona: str


@dataclass
class ContextOptions:
    """Input options that drive ``build_fleet_context``.

    All fields have safe defaults so callers can use keyword-only construction
    without specifying every field.
    """

    workspace_arg: str | None = None
    """CLI --workspace value (None → use cwd)."""

    config_arg: str | None = None
    """CLI --config value (None → load_fleet_config with no path, i.e. default)."""

    persona_arg: str | None = None
    """CLI --persona value (None → repo default → fleet config default)."""

    backend_arg: str | None = None
    """CLI --backend value (None → env AGENT_FLEET_BACKEND → fleet.yaml)."""

    model_arg: str | None = None
    """CLI --model value (None → env AGENT_FLEET_MODEL → fleet.yaml / backend default)."""

    require_env: bool = False
    """When True, check that the backend's required API key is present.

    Commands that actually call a backend (run, review, scope, scout, loop,
    learn) set this to True.  Read-only or dry-run paths leave it False so
    they never block on a missing key.
    """

    use_env_target_config: bool = False
    """When True, honour ``AGENT_FLEET_TARGET_CONFIG`` for repo discovery.

    Only the issue-dispatch path opts in.  All other commands use
    ``find_repo_config`` semantics so they see the repo whose directory
    contains the invocation workspace.
    """

    personas_dir_from_repo: bool = False
    """When True, apply repo.personas_dir onto config.personas_dir if present.

    Commands that load persona prompts (run, loop, learn) set this to True.
    """


def build_fleet_context(
    opts: ContextOptions,
) -> tuple[FleetContext, None] | tuple[None, int]:
    """Build a FleetContext from *opts*, or return ``(None, exit_code)`` on error.

    The caller should check the second element:

        ctx, err = build_fleet_context(opts)
        if err is not None:
            return err

    No exceptions are raised; all expected failure modes produce an exit code.
    """
    from agent_fleet.cli_env import require_backend_env
    from agent_fleet.config import load_fleet_config
    from agent_fleet.repo import find_repo_config, resolve_repo_config

    # 1. Workspace: explicit arg beats cwd.
    workspace = Path(opts.workspace_arg or Path.cwd()).resolve()

    # 2. Config: always call load_fleet_config — accepts None as "use default".
    #    CLI --backend / --model kwargs beat env + yaml (see load_fleet_config).
    config = load_fleet_config(
        opts.config_arg,
        default_backend=opts.backend_arg.lower().strip() if opts.backend_arg else None,
        default_model=opts.model_arg.strip() if opts.model_arg else None,
    )

    # 3. Repo discovery: env-target path vs. standard walk-up search.
    if opts.use_env_target_config:
        repo = resolve_repo_config(workspace)
    else:
        repo = find_repo_config(workspace)

    # 4. Optional personas_dir override from repo.
    if opts.personas_dir_from_repo and repo is not None and repo.personas_dir is not None:
        config.personas_dir = repo.personas_dir

    # 5. Persona fallback chain: arg → repo default → fleet config default.
    if opts.persona_arg:
        persona = opts.persona_arg
    elif repo is not None and repo.default_persona:
        persona = repo.default_persona
    else:
        persona = config.default_persona

    # 6. Backend env guard (only when require_env=True).
    if opts.require_env and (code := require_backend_env(config)) is not None:
        return None, code

    return FleetContext(workspace=workspace, config=config, repo=repo, persona=persona), None
