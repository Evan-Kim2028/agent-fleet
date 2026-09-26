"""command_timeout_seconds must bound a command that backgrounds a child.

``_run_command`` kills only the pid it started.  A command that backgrounds
work leaves a grandchild holding the inherited stdout/stderr pipe write ends,
so the follow-up ``proc.communicate()`` blocks until that orphan exits.  The
timeout ceiling documented at docs/MERGE-PLAN.md:271-272 is then not a ceiling
at all: the call blocks for the orphan's whole remaining lifetime.
"""

from __future__ import annotations

import time

from agent_fleet.merge_plan.execute import TIMEOUT_EXIT_CODE, _run_command


def test_backgrounded_child_does_not_defeat_the_timeout_ceiling() -> None:
    started = time.monotonic()
    result = _run_command(
        ["sh", "-c", "sleep 20 & wait"],
        cwd=None,
        timeout=2,
        dry_run=False,
    )
    elapsed = time.monotonic() - started

    assert result.timed_out is True
    assert result.returncode == TIMEOUT_EXIT_CODE
    # The command overran its 2s ceiling.  Nothing may keep the executor
    # blocked after that -- the orphaned grandchild must not hold the pipes.
    assert elapsed < 8.0, f"executor blocked {elapsed:.1f}s past a 2s timeout"
