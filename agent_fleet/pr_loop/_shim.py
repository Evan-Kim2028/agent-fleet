"""Thin deprecation shim for the agent-fleet-pr-loop console script.

Prepends "loop" to argv and delegates to ``agent_fleet.cli:main``.  The
unified entry point is ``fleet loop``; this shim keeps the old console-script
name alive so existing CI configurations continue to work without updates.
"""

from __future__ import annotations

import sys


def main() -> int:
    # Inject "loop" as the first positional so the unified parser routes to
    # cmd_loop, then pass the rest of the original argv unchanged.
    from agent_fleet.cli import main as fleet_main

    sys.argv = [sys.argv[0], "loop", *sys.argv[1:]]
    return fleet_main()


if __name__ == "__main__":
    raise SystemExit(main())
