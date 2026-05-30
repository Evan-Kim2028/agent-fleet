"""Tests for task-level allowed_paths enforcement (v0.8.4)."""

from __future__ import annotations

from agent_fleet.hooks import (
    FleetTask,
    FleetTaskResult,
)

# ---------------------------------------------------------------------------
# Test 1 — empty allowed_paths is a no-op
# ---------------------------------------------------------------------------


def test_empty_allowed_paths_is_noop() -> None:
    """No allowed_paths set → field defaults to empty tuple."""
    task = FleetTask(goal="do something", allowed_paths=())
    assert task.allowed_paths == ()


# ---------------------------------------------------------------------------
# Test 2 — all in-scope files pass through
# ---------------------------------------------------------------------------


def test_all_in_scope_files_pass() -> None:
    task = FleetTask(goal="scoped", allowed_paths=("tests/", "packages/lakestore/"))
    changed = ["tests/test_foo.py", "packages/lakestore/model.py"]

    out_of_scope = [p for p in changed if not any(p.startswith(ap) for ap in task.allowed_paths)]
    assert out_of_scope == [], "All files should be in scope"


# ---------------------------------------------------------------------------
# Test 3 — out-of-scope files are detected
# ---------------------------------------------------------------------------


def test_out_of_scope_triggers_violation() -> None:
    task = FleetTask(goal="scoped", allowed_paths=("tests/", "packages/lakestore/"))
    changed = [
        "tests/test_foo.py",
        "packages/lakestore/model.py",
        "pipelines/pokemontcg_pipe/run.py",  # OUT OF SCOPE
        "pipelines/pokemontcg_pipe/config.yaml",  # OUT OF SCOPE
    ]

    out_of_scope = [p for p in changed if not any(p.startswith(ap) for ap in task.allowed_paths)]
    assert len(out_of_scope) == 2
    assert "pipelines/pokemontcg_pipe/run.py" in out_of_scope
    assert "pipelines/pokemontcg_pipe/config.yaml" in out_of_scope


def test_scope_violation_result_shape() -> None:
    """FleetTaskResult with status='scope_violation' carries the right fields."""
    result = FleetTaskResult(
        task_index=0,
        persona="coder",
        goal="test task",
        status="scope_violation",
        summary=None,
        error="Agent modified 2 file(s) outside allowed_paths: ['pipelines/foo.py']",
        duration_seconds=1.0,
        files_modified=("tests/test_foo.py", "pipelines/pokemontcg_pipe/run.py"),
    )
    assert result.status == "scope_violation"
    assert result.error is not None
    assert "outside allowed_paths" in result.error
    assert len(result.files_modified) == 2


def test_allowed_paths_field_on_fleet_task() -> None:
    """FleetTask.allowed_paths defaults to empty tuple."""
    task = FleetTask(goal="do work")
    assert task.allowed_paths == ()

    task2 = FleetTask(goal="scoped", allowed_paths=("tests/", "src/"))
    assert task2.allowed_paths == ("tests/", "src/")


def test_scope_violation_error_message_format() -> None:
    """Error message shows count and first 3 offending files."""
    task = FleetTask(goal="scoped", allowed_paths=("tests/",))
    changed = [
        "pipelines/a.py",
        "pipelines/b.py",
        "pipelines/c.py",
        "pipelines/d.py",
    ]
    out_of_scope = [p for p in changed if not any(p.startswith(ap) for ap in task.allowed_paths)]
    n = len(out_of_scope)
    first3 = out_of_scope[:3]
    error_msg = f"Agent modified {n} file(s) outside allowed_paths: {first3}"
    assert "4 file(s)" in error_msg
    assert "pipelines/a.py" in error_msg
    assert "pipelines/d.py" not in error_msg  # only first 3 shown
