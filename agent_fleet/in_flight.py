"""Track and reap issue-dispatch subprocesses in watcher state."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def pid_is_dispatch(pid: int) -> bool:
    try:
        with Path(f"/proc/{pid}/cmdline").open("rb") as handle:
            return b"agent_fleet.issue_loop.dispatch" in handle.read()
    except FileNotFoundError, ProcessLookupError, PermissionError:
        return False


def reap_in_flight(state: dict[str, Any]) -> int:
    """Drop finished dispatch PIDs and empty issue keys. Returns reaped run count."""
    in_flight = state.setdefault("in_flight", {})
    reaped = 0
    for issue_key, runs in list(in_flight.items()):
        if not isinstance(runs, list) or not runs:
            in_flight.pop(issue_key, None)
            continue
        alive = [run for run in runs if pid_is_dispatch(int(run["pid"]))]
        reaped += len(runs) - len(alive)
        if alive:
            in_flight[issue_key] = alive
        else:
            in_flight.pop(issue_key, None)
    if reaped:
        logger.info("Reaped %s finished dispatch run(s) from in_flight", reaped)
    return reaped
