"""agent_fleet — generic agentic coding fleet with Cursor SDK backend."""

from agent_fleet.config import FleetConfig, load_fleet_config
from agent_fleet.dispatcher import FleetDispatcher, dispatch_tasks
from agent_fleet.repo import RepoConfig, find_repo_config, load_repo_config
from agent_fleet.runner import FleetRunResult, LocalFleetRunner, run_full_pipeline

__all__ = [
    "FleetConfig",
    "FleetDispatcher",
    "FleetRunResult",
    "LocalFleetRunner",
    "RepoConfig",
    "dispatch_tasks",
    "find_repo_config",
    "load_fleet_config",
    "load_repo_config",
    "run_full_pipeline",
]

__version__ = "0.8.5"
