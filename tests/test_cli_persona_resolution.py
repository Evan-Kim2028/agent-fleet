"""Regression test for cmd_run persona resolution (arg -> repo default -> fleet default)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from agent_fleet.config import FleetConfig


def test_cmd_run_dispatch_uses_repo_default_persona_not_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fleet run`` without --persona must use the repo's default_persona.

    Regression for a bug where cmd_run passed the raw (unresolved) CLI
    ``--persona`` value straight to ``FleetDispatcher.dispatch()`` instead of
    ``ctx.persona`` (which already applies the arg -> repo default_persona ->
    fleet config default_persona fallback chain used by --dry-run). Without
    --persona, this silently fell through to the *global* fleet.yaml
    default_persona instead of the repo's, running the wrong (unscoped)
    verify_commands for that persona.
    """
    from agent_fleet import cli, devin_backend

    # This test is about persona resolution, not auth. Without this stub it
    # passes only on a machine that happens to have real Devin credentials
    # and fails in CI, where ~/.local/share/devin/credentials.toml is absent
    # and require_backend_env() short-circuits cmd_run before it ever
    # dispatches.
    monkeypatch.setattr(devin_backend, "check_devin_auth", lambda: (True, "stubbed", ""))

    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".agent-fleet.yaml").write_text(
        "default_persona: repo-persona-sentinel\n", encoding="utf-8"
    )
    fleet_yaml = tmp_path / "fleet.yaml"
    fleet_yaml.write_text(
        "default_persona: global-persona-sentinel\ndefault_backend: devin\n",
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    class _FakeDispatcher:
        def __init__(self, *, config: FleetConfig) -> None:
            captured["config_default_persona"] = config.default_persona

        def dispatch(self, **kwargs: object) -> list[object]:
            captured.update(kwargs)
            return []

    monkeypatch.setattr(cli, "FleetDispatcher", _FakeDispatcher)

    rc = cli.main(
        [
            "--config",
            str(fleet_yaml),
            "run",
            "--workspace",
            str(workspace),
            "--backend",
            "devin",
            "hello",
        ]
    )

    del rc  # empty dispatch result -> emit() exit code isn't the point of this test
    assert captured["persona"] == "repo-persona-sentinel"
