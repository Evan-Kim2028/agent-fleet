"""Regression: `fleet run` (FleetDispatcher.dispatch) must register in the
run index (index.jsonl, read by `fleet runs`), not just write its own
per-run <run_id>.jsonl event stream.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from agent_fleet.noop_session import NoopLLMResult

if TYPE_CHECKING:
    import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_dispatch_registers_run_in_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: FleetDispatcher._execute_task wrote its own <run_id>.jsonl
    event stream via FleetLogger.for_dispatch, but never called
    RunLog.run_start/run_end — the only two methods that append to
    index.jsonl. Only runner.py's issue-driven runs called them, so `fleet
    runs` silently never showed a plain `fleet run` invocation, regardless
    of how it finished (including a successful one)."""
    import agent_fleet.observability.log as log_module
    from agent_fleet.config import load_fleet_config
    from agent_fleet.dispatcher import FleetDispatcher
    from agent_fleet.observability.run_store import read_run_index

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    monkeypatch.setattr(log_module, "_DEFAULT_RUNS_DIR", runs_dir)

    legacy = MagicMock(spec=["run"])
    legacy.run.return_value = NoopLLMResult(
        stdout="ok", stderr="", exit_code=0, duration_s=0.1, agent_id=None
    )
    fc = load_fleet_config(ROOT / "fleet.example.yaml")
    fc.default_workspace = str(tmp_path)
    dispatcher = FleetDispatcher(config=fc)
    dispatcher.backend = legacy  # type: ignore[assignment]

    monkeypatch.setattr(
        "agent_fleet.dispatcher_task.should_isolate_worktree", lambda *_a, **_k: False
    )

    results = dispatcher.dispatch(
        goal="dejargon README for issue #2382",
        persona="coder",
        workspace=str(tmp_path),
        pipeline="simple",
    )
    assert len(results) == 1

    rows = read_run_index(runs_dir=runs_dir)
    assert len(rows) == 1, f"expected exactly one index row, got {rows}"
    row = rows[0]
    assert row["goal"] == "dejargon README for issue #2382"
    assert row["status"] != "running", (
        f"run_start opened the index row but run_end never closed it — row stuck as {row!r}"
    )


def test_dispatch_closes_index_row_even_when_admission_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every early-return path in _execute_task must also close the
    index.jsonl row run_start opened — an unmatched run_start is worse than
    the original bug (no row at all): it leaves a permanently "running"
    ghost entry in `fleet runs` that never resolves."""
    import agent_fleet.observability.log as log_module
    from agent_fleet.admission import AdmissionDenied
    from agent_fleet.config import load_fleet_config
    from agent_fleet.dispatcher import FleetDispatcher
    from agent_fleet.observability.run_store import read_run_index

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    monkeypatch.setattr(log_module, "_DEFAULT_RUNS_DIR", runs_dir)

    fc = load_fleet_config(ROOT / "fleet.example.yaml")
    fc.default_workspace = str(tmp_path)
    dispatcher = FleetDispatcher(config=fc)
    dispatcher.backend = MagicMock(spec=["run"])

    def _deny(*_a: object, **_k: object) -> None:
        raise AdmissionDenied("nesting depth exceeds capacity")

    monkeypatch.setattr(dispatcher._gate, "acquire_token", _deny)

    results = dispatcher.dispatch(
        goal="denied task",
        persona="coder",
        workspace=str(tmp_path),
        pipeline="simple",
    )
    assert len(results) == 1
    assert results[0].status == "error"

    # Every admission-denied attempt (the dispatcher may redispatch/retry
    # internally) must close its own index row — none may be left "running".
    rows = read_run_index(runs_dir=runs_dir)
    assert rows, "expected at least one index row"
    for row in rows:
        assert row["status"] == "error", f"index row stuck non-terminal: {row!r}"
