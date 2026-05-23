"""Automated PR lifecycle: review → fix → CI green → merge."""

from agent_fleet.pr_loop.config import PrLoopConfig, load_pr_loop_config

__all__ = [
    "PrLoopConfig",
    "PrLoopWatcher",
    "load_pr_loop_config",
    "run_pr_lifecycle",
    "run_watcher_once",
]


def __getattr__(name: str) -> object:
    if name == "run_pr_lifecycle":
        from agent_fleet.pr_loop.lifecycle import run_pr_lifecycle

        return run_pr_lifecycle
    if name == "PrLoopWatcher":
        from agent_fleet.pr_loop.watcher import PrLoopWatcher

        return PrLoopWatcher
    if name == "run_watcher_once":
        from agent_fleet.pr_loop.watcher import run_watcher_once

        return run_watcher_once
    raise AttributeError(name)
