"""Hermes plugin tool handlers for Cursor coding fleet."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_fleet.config import FleetConfig
    from agent_fleet.dispatcher import FleetDispatcher
    from agent_fleet.personas import YamlPersonaResolver

logger = logging.getLogger(__name__)


class _ParentAgent(Protocol):
    tool_progress_callback: Callable[..., None] | None


class _ProgressRelay(Protocol):
    def __call__(self, event: str, **payload: object) -> None: ...


def _ensure_agent_fleet() -> tuple[
    Callable[..., FleetConfig],
    type[FleetDispatcher],
    type[YamlPersonaResolver],
]:
    try:
        from agent_fleet.config import load_fleet_config
        from agent_fleet.dispatcher import FleetDispatcher
        from agent_fleet.personas import YamlPersonaResolver

        return load_fleet_config, FleetDispatcher, YamlPersonaResolver
    except ImportError as exc:
        raise RuntimeError(
            "agent-fleet package not installed. Run: pip install -e /path/to/agent_fleet"
        ) from exc


def _build_progress_callback(
    parent_agent: object | None,
) -> _ProgressRelay | None:
    if parent_agent is None:
        return None
    cb = getattr(parent_agent, "tool_progress_callback", None)
    if not cb:
        return None

    def relay(event: str, **payload: object) -> None:
        try:
            cb(event, **payload)
        except Exception as exc:
            logger.debug("Progress relay failed: %s", exc)

    return relay


def _optional_str(value: object | None) -> str | None:
    return str(value) if value is not None else None


def _optional_task_list(value: object | None) -> list[dict[str, object]] | None:
    if not isinstance(value, list):
        return None
    tasks: list[dict[str, object]] = []
    for item in value:
        if isinstance(item, dict):
            tasks.append({str(key): val for key, val in item.items()})
    return tasks


def coding_fleet_dispatch(args: dict[str, object], **kwargs: object) -> str:
    parent_agent = kwargs.get("parent_agent")
    try:
        load_fleet_config, FleetDispatcher, _ = _ensure_agent_fleet()
    except RuntimeError as exc:
        return json.dumps({"error": str(exc)})

    config_path_raw = args.get("config_path") or os.environ.get("CODING_FLEET_CONFIG")
    config_path = _optional_str(config_path_raw)
    config = load_fleet_config(config_path) if config_path else load_fleet_config()

    backend_name = config.default_backend.lower()
    if backend_name == "cursor" and not os.environ.get("CURSOR_API_KEY"):
        return json.dumps(
            {
                "error": "CURSOR_API_KEY is not set. "
                "Add it to ~/.hermes/.env (see https://cursor.com/dashboard/integrations)"
            }
        )
    if backend_name == "kimi" and not os.environ.get("KIMI_API_KEY"):
        return json.dumps(
            {
                "error": "KIMI_API_KEY is not set. "
                "Use Kimi Code subscription key (https://platform.kimi.ai) "
                "or set default_backend: cursor in fleet.yaml"
            }
        )

    dispatcher = FleetDispatcher(
        config=config,
        progress_callback=_build_progress_callback(parent_agent),
    )

    tasks = _optional_task_list(args.get("tasks"))

    try:
        results = dispatcher.dispatch(
            goal=_optional_str(args.get("goal")),
            context=_optional_str(args.get("context")),
            persona=_optional_str(args.get("persona")),
            workspace=_optional_str(args.get("workspace")),
            pipeline=_optional_str(args.get("pipeline")),
            tasks=tasks,
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    return json.dumps(
        {
            "results": [asdict(r) for r in results],
            "personas_available": dispatcher.resolver.list_personas(),
            "pipelines_available": list(config.pipelines.keys()),
        }
    )


def coding_fleet_pr_review(args: dict[str, object], **kwargs: object) -> str:
    del kwargs
    try:
        load_fleet_config, _, _ = _ensure_agent_fleet()
    except RuntimeError as exc:
        return json.dumps({"error": str(exc)})

    config_path_raw = args.get("config_path") or os.environ.get("CODING_FLEET_CONFIG")
    config_path = _optional_str(config_path_raw)
    config = load_fleet_config(config_path) if config_path else load_fleet_config()

    backend_name = config.default_backend.lower()
    if backend_name == "cursor" and not os.environ.get("CURSOR_API_KEY"):
        return json.dumps(
            {
                "error": "CURSOR_API_KEY is not set. "
                "Add it to ~/.hermes/.env (see https://cursor.com/dashboard/integrations)"
            }
        )
    if backend_name == "kimi" and not os.environ.get("KIMI_API_KEY"):
        return json.dumps(
            {
                "error": "KIMI_API_KEY is not set. "
                "Use Kimi Code subscription key (https://platform.kimi.ai) "
                "or set default_backend: cursor in fleet.yaml"
            }
        )

    workspace_raw = args.get("workspace")
    if not workspace_raw:
        return json.dumps({"error": "workspace is required"})
    workspace = Path(str(workspace_raw)).expanduser().resolve()

    from agent_fleet.pr_review.runner import run_pr_review

    try:
        result = run_pr_review(
            workspace=workspace,
            fleet_config=config,
            base_branch=str(args.get("base_branch") or "main"),
            pr_number=int(args.get("pr_number") or 0),
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})

    output_format = str(args.get("output_format") or "json").lower()
    if output_format == "comment":
        return str(result.get("comment_markdown") or "")
    return json.dumps(result, default=str)


def coding_fleet_pr_loop(args: dict[str, object], **kwargs: object) -> str:
    del kwargs
    try:
        load_fleet_config, _, _ = _ensure_agent_fleet()
    except RuntimeError as exc:
        return json.dumps({"error": str(exc)})

    config_path_raw = args.get("config_path") or os.environ.get("CODING_FLEET_CONFIG")
    config_path = _optional_str(config_path_raw)
    config = load_fleet_config(config_path) if config_path else load_fleet_config()

    backend_name = config.default_backend.lower()
    if backend_name == "cursor" and not os.environ.get("CURSOR_API_KEY"):
        return json.dumps(
            {
                "error": "CURSOR_API_KEY is not set. "
                "Add it to ~/.hermes/.env (see https://cursor.com/dashboard/integrations)"
            }
        )
    if backend_name == "kimi" and not os.environ.get("KIMI_API_KEY"):
        return json.dumps(
            {
                "error": "KIMI_API_KEY is not set. "
                "Use Kimi Code subscription key (https://platform.kimi.ai) "
                "or set default_backend: cursor in fleet.yaml"
            }
        )

    workspace_raw = args.get("workspace")
    if not workspace_raw:
        return json.dumps({"error": "workspace is required"})
    workspace = Path(str(workspace_raw)).expanduser().resolve()

    mode = str(args.get("mode") or "once").lower()
    if mode not in {"once", "pr"}:
        return json.dumps({"error": "mode must be 'once' or 'pr'"})

    from agent_fleet.pr_loop.config import load_pr_loop_config
    from agent_fleet.repo import find_repo_config
    import yaml

    repo = find_repo_config(workspace)
    if repo is None:
        return json.dumps({"error": f"No .agent-fleet.yaml under {workspace}"})

    raw: dict[str, object] = {}
    for name in (".agent-fleet.yaml", ".agent-fleet.yml"):
        path = repo.repo_root / name
        if path.exists():
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            break
    loop_config = load_pr_loop_config(repo.repo_root, raw)
    if loop_config is None or not loop_config.enabled:
        return json.dumps({"error": "pr_loop.enabled is not true in .agent-fleet.yaml"})

    if mode == "once":
        from agent_fleet.pr_loop.watcher import run_watcher_once

        try:
            results = run_watcher_once(workspace)
        except Exception as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps({"mode": "once", "results": results})

    pr_number_raw = args.get("pr_number")
    if not pr_number_raw:
        return json.dumps({"error": "pr_number is required when mode=pr"})
    pr_number = int(pr_number_raw)

    branch = _optional_str(args.get("branch"))
    if not branch:
        import subprocess

        result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "headRefName"],
            capture_output=True,
            text=True,
            check=False,
            cwd=workspace,
        )
        if result.returncode != 0:
            return json.dumps({"error": "branch required or gh must resolve PR head"})
        branch = json.loads(result.stdout).get("headRefName", "")

    from agent_fleet.pr_loop.lifecycle import run_pr_lifecycle

    skip_review_wait = args.get("skip_review_wait")
    if skip_review_wait is None:
        skip_review_wait = True
    else:
        skip_review_wait = bool(skip_review_wait)

    try:
        outcome = run_pr_lifecycle(
            pr_number=pr_number,
            branch=str(branch),
            repo=repo,
            loop_config=loop_config,
            fleet_config=config,
            skip_review_wait=skip_review_wait,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})

    return json.dumps(
        {
            "mode": "pr",
            "pr_number": pr_number,
            "branch": branch,
            "status": outcome.status,
            "detail": outcome.detail,
        }
    )


def coding_fleet_list_personas(args: dict[str, object], **kwargs: object) -> str:
    del args, kwargs
    try:
        load_fleet_config, _, YamlPersonaResolver = _ensure_agent_fleet()
    except RuntimeError as exc:
        return json.dumps({"error": str(exc)})

    config = load_fleet_config()
    resolver = YamlPersonaResolver(config)
    return json.dumps(
        {
            "personas": resolver.list_personas(),
            "pipelines": config.pipelines,
            "default_model": config.default_model,
            "max_parallel": config.max_parallel,
            "config_path": str(Path.home() / ".hermes" / "coding_fleet" / "fleet.yaml"),
        }
    )
