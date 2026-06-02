"""Regression: dispatcher must forward loadout_size from runtime to resolve_dispatch_equip.

Before the fix, the call omitted loadout_size, silently passing None (full loadout)
even when the complexity tier derived a smaller size.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from agent_fleet.config import load_fleet_config
from agent_fleet.dispatcher import FleetDispatcher
from agent_fleet.level_up.models import DispatchEquip

if TYPE_CHECKING:
    import pytest

ROOT = Path(__file__).resolve().parent.parent

_STUB_EQUIP = DispatchEquip(
    skill_slots_execute=(),
    skill_slots_review=(),
    level_up_generation=0,
    compose_body="",
)


def _dispatcher(tmp_path: Path) -> FleetDispatcher:
    fc = load_fleet_config(ROOT / "fleet.example.yaml")
    fc.default_workspace = str(tmp_path)
    return FleetDispatcher(config=fc)


def test_dispatcher_forwards_tier_loadout_size_to_resolve_dispatch_equip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dispatcher path must pass loadout_size from the derived runtime, not None."""
    captured: list[str | None] = []

    def _fake_resolve_dispatch_equip(
        task,  # noqa: ANN001, ARG001
        fleet_config,  # noqa: ANN001, ARG001
        repo,  # noqa: ANN001, ARG001
        run_id=None,  # noqa: ANN001, ARG001
        loadout_size=None,  # noqa: ANN001
    ) -> DispatchEquip:
        captured.append(loadout_size)
        return _STUB_EQUIP

    backend = MagicMock()
    backend.run.return_value = MagicMock(
        stdout="done", stderr="", exit_code=0, duration_s=0.1, agent_id=None
    )

    dispatcher = _dispatcher(tmp_path)
    dispatcher.backend = backend  # type: ignore[assignment]

    monkeypatch.setattr(
        "agent_fleet.dispatcher_task.should_isolate_worktree", lambda *_a, **_k: False
    )

    with patch(
        "agent_fleet.orchestration.equip.resolve_dispatch_equip",
        side_effect=_fake_resolve_dispatch_equip,
    ):
        dispatcher.dispatch(
            goal="test task",
            persona="coder",
            workspace=str(tmp_path),
            complexity="LOW",
        )

    assert len(captured) == 1, f"resolve_dispatch_equip should be called once, got {len(captured)}"
    assert captured[0] == "minimal", (
        f"LOW complexity tier should forward loadout_size='minimal', got {captured[0]!r}"
    )
