"""Hermes plugin tool handlers for Cursor coding fleet."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _ensure_agent_fleet():
    try:
        from agent_fleet.config import load_fleet_config
        from agent_fleet.dispatcher import FleetDispatcher
        from agent_fleet.personas import YamlPersonaResolver

        return load_fleet_config, FleetDispatcher, YamlPersonaResolver
    except ImportError as exc:
        raise RuntimeError(
            "agent-fleet package not installed. Run: pip install -e /path/to/agent_fleet"
        ) from exc


def _build_progress_callback(parent_agent: Any):
    if parent_agent is None:
        return None
    cb = getattr(parent_agent, "tool_progress_callback", None)
    if not cb:
        return None

    def relay(event: str, **payload: Any) -> None:
        try:
            cb(event, **payload)
        except Exception as exc:
            logger.debug("Progress relay failed: %s", exc)

    return relay


def coding_fleet_dispatch(args: dict, **kwargs) -> str:
    parent_agent = kwargs.get("parent_agent")
    try:
        load_fleet_config, FleetDispatcher, _ = _ensure_agent_fleet()
    except RuntimeError as exc:
        return json.dumps({"error": str(exc)})

    if not os.environ.get("CURSOR_API_KEY"):
        return json.dumps(
            {
                "error": "CURSOR_API_KEY is not set. "
                "Add it to ~/.hermes/.env (see https://cursor.com/dashboard/integrations)"
            }
        )

    config_path = args.get("config_path") or os.environ.get("CODING_FLEET_CONFIG")
    config = load_fleet_config(config_path) if config_path else load_fleet_config()

    dispatcher = FleetDispatcher(
        config=config,
        progress_callback=_build_progress_callback(parent_agent),
    )

    try:
        results = dispatcher.dispatch(
            goal=args.get("goal"),
            context=args.get("context"),
            persona=args.get("persona"),
            workspace=args.get("workspace"),
            pipeline=args.get("pipeline"),
            tasks=args.get("tasks"),
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


def coding_fleet_list_personas(args: dict, **kwargs) -> str:
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
