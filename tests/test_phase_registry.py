"""Phase registry: validate_phases rejects unknown names at pipeline-resolve time."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_fleet.config import load_fleet_config
from agent_fleet.hooks import FleetTask
from agent_fleet.personas import YamlPersonaResolver
from agent_fleet.phases import run_pipeline, validate_phases

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# validate_phases unit tests
# ---------------------------------------------------------------------------


def test_validate_phases_accepts_known_phases() -> None:
    # Should not raise for any registered phase name.
    validate_phases(["execute"])
    validate_phases(["analyze"])
    validate_phases(["review"])
    validate_phases(["execute", "review"])


def test_validate_phases_rejects_unknown_single() -> None:
    with pytest.raises(ValueError, match="unknown_phase"):
        validate_phases(["unknown_phase"])


def test_validate_phases_rejects_unknown_lists_known_phases() -> None:
    with pytest.raises(ValueError, match="execute") as exc_info:
        validate_phases(["bogus"])
    # Error message names the known phases so users know what's valid.
    assert "analyze" in str(exc_info.value)
    assert "review" in str(exc_info.value)


def test_validate_phases_rejects_multiple_unknown() -> None:
    with pytest.raises(ValueError, match="foo") as exc_info:
        validate_phases(["execute", "foo", "bar"])
    assert "bar" in str(exc_info.value)


def test_validate_phases_empty_list_ok() -> None:
    validate_phases([])


# ---------------------------------------------------------------------------
# Each known phase is accepted by validate_phases
# ---------------------------------------------------------------------------


def test_validate_phases_accepts_each_known_phase() -> None:
    # Every phase run_pipeline can dispatch must pass validation.
    for phase in ("execute", "analyze", "review"):
        validate_phases([phase])


# ---------------------------------------------------------------------------
# run_pipeline rejects unknown phase at entry (before any agent call)
# ---------------------------------------------------------------------------


def test_run_pipeline_rejects_unknown_phase_before_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown phase name must fail early, not silently mid-run."""
    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    resolver = YamlPersonaResolver(fleet_config)
    task = FleetTask(goal="do something", persona="coder")

    # Patch the backend so it would error if any phase actually ran.
    import agent_fleet.phases as _phases

    monkeypatch.setattr(
        _phases,
        "run_execute_phase",
        lambda **_kw: (_ for _ in ()).throw(AssertionError("should not run")),
    )

    with pytest.raises(ValueError, match="no_such_phase"):
        run_pipeline(
            backend=None,  # ty: ignore[invalid-argument-type]
            resolver=resolver,
            task=task,
            workspace=tmp_path,
            timeout_s=30,
            phases=["no_such_phase"],
        )
