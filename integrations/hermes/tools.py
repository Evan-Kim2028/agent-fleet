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
