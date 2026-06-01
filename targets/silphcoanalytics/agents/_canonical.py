"""Refuses to launch fleet entrypoints from a non-canonical checkout.

Background: a parallel runtime (e.g. the takopi runtime's isolated clone at
/home/evan/Documents/takopi_adventures/projects/silphcoanalytics/agents) has
its own .venv whose entry points import the takopi-pathed package. If such a
runtime launches `agents.dispatch`, two daemons end up polling the same GitHub
repo and dispatching competing fleet runs. This guard exits early in that case
so only the canonical evan-owned systemd service can produce fleet activity.
"""

import sys
from pathlib import Path

CANONICAL_ROOT = Path("/home/evan/Documents/silphcoanalytics/agents").resolve()


def assert_canonical_checkout() -> None:
    loaded_root = Path(__file__).resolve().parent.parent
    if loaded_root == CANONICAL_ROOT:
        return
    sys.stderr.write(
        "FATAL: agent-fleet entrypoint refuses to run from a non-canonical checkout.\n"
        f"  loaded source: {loaded_root}\n"
        f"  canonical:     {CANONICAL_ROOT}\n"
        "The canonical watcher is the systemd user service 'agent-fleet-watch.service'.\n"
        "If it is down, run: systemctl --user start agent-fleet-watch.service\n"
    )
    sys.exit(2)
