"""Unit tests for agent_fleet.context — build_fleet_context."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from agent_fleet.context import ContextOptions, build_fleet_context

if TYPE_CHECKING:
    import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _opts(
    workspace_arg: str | None = None,
    config_arg: str | None = None,
    persona_arg: str | None = None,
    require_env: bool = False,
    use_env_target_config: bool = False,
    personas_dir_from_repo: bool = False,
) -> ContextOptions:
    """Build ContextOptions with sane defaults for tests."""
    return ContextOptions(
        workspace_arg=workspace_arg,
        config_arg=config_arg,
        persona_arg=persona_arg,
        require_env=require_env,
        use_env_target_config=use_env_target_config,
        personas_dir_from_repo=personas_dir_from_repo,
    )


# ---------------------------------------------------------------------------
# workspace defaulting
# ---------------------------------------------------------------------------


def test_workspace_defaults_to_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    ctx, err = build_fleet_context(_opts())
    assert err is None
    assert ctx is not None
    assert ctx.workspace == tmp_path.resolve()


def test_workspace_arg_overrides_cwd(tmp_path: Path) -> None:
    ctx, err = build_fleet_context(_opts(workspace_arg=str(tmp_path)))
    assert err is None
    assert ctx is not None
    assert ctx.workspace == tmp_path.resolve()


# ---------------------------------------------------------------------------
# config-guard collapse: load_fleet_config called without redundant if-guard
# ---------------------------------------------------------------------------


def test_config_arg_none_loads_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """config_arg=None must still call load_fleet_config (no early-return guard)."""
    monkeypatch.chdir(tmp_path)
    ctx, err = build_fleet_context(_opts(config_arg=None))
    assert err is None
    assert ctx is not None
    assert ctx.config is not None


def test_config_arg_path_is_used(tmp_path: Path) -> None:
    """When config_arg points to a valid YAML, that config is loaded."""
    cfg = tmp_path / "fleet.yaml"
    cfg.write_text("default_backend: kimi\n", encoding="utf-8")
    ctx, err = build_fleet_context(_opts(config_arg=str(cfg)))
    assert err is None
    assert ctx is not None
    assert ctx.config.default_backend == "kimi"


# ---------------------------------------------------------------------------
# require_env: on/off behaviour
# ---------------------------------------------------------------------------


def test_require_env_off_does_not_check_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With require_env=False, missing API keys must not cause an error exit."""
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    ctx, err = build_fleet_context(_opts(workspace_arg=str(tmp_path), require_env=False))
    assert err is None
    assert ctx is not None


def test_require_env_on_returns_exit_code_when_key_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With require_env=True and a cursor backend, missing CURSOR_API_KEY -> exit 1."""
    # Need a config that specifies cursor backend explicitly
    cfg = tmp_path / "fleet.yaml"
    cfg.write_text("default_backend: cursor\n", encoding="utf-8")
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)

    ctx, err = build_fleet_context(
        _opts(workspace_arg=str(tmp_path), config_arg=str(cfg), require_env=True)
    )
    assert ctx is None
    assert err == 1


def test_require_env_on_passes_when_key_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "fleet.yaml"
    cfg.write_text("default_backend: cursor\n", encoding="utf-8")
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")

    ctx, err = build_fleet_context(
        _opts(workspace_arg=str(tmp_path), config_arg=str(cfg), require_env=True)
    )
    assert err is None
    assert ctx is not None


# ---------------------------------------------------------------------------
# persona fallback chain
# ---------------------------------------------------------------------------


def test_persona_from_opts_overrides_config(tmp_path: Path) -> None:
    cfg = tmp_path / "fleet.yaml"
    cfg.write_text("default_persona: reviewer\n", encoding="utf-8")
    ctx, err = build_fleet_context(
        _opts(workspace_arg=str(tmp_path), config_arg=str(cfg), persona_arg="coder")
    )
    assert err is None
    assert ctx is not None
    assert ctx.persona == "coder"


def test_persona_falls_back_to_repo_then_config(tmp_path: Path) -> None:
    """When persona_arg is None, persona comes from repo default_persona."""
    repo_yaml = tmp_path / ".agent-fleet.yaml"
    repo_yaml.write_text("default_persona: tech-scout\n", encoding="utf-8")
    fleet_cfg = tmp_path / "fleet.yaml"
    fleet_cfg.write_text("default_persona: reviewer\n", encoding="utf-8")

    ctx, err = build_fleet_context(
        _opts(workspace_arg=str(tmp_path), config_arg=str(fleet_cfg), persona_arg=None)
    )
    assert err is None
    assert ctx is not None
    # repo's default_persona should win over fleet config
    assert ctx.persona == "tech-scout"


def test_persona_falls_back_to_fleet_config_when_no_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No .agent-fleet.yaml → persona comes from fleet config."""
    monkeypatch.chdir(tmp_path)
    fleet_cfg = tmp_path / "fleet.yaml"
    fleet_cfg.write_text("default_persona: pr-analyzer\n", encoding="utf-8")

    ctx, err = build_fleet_context(
        _opts(workspace_arg=str(tmp_path), config_arg=str(fleet_cfg), persona_arg=None)
    )
    assert err is None
    assert ctx is not None
    assert ctx.persona == "pr-analyzer"


# ---------------------------------------------------------------------------
# personas_dir_from_repo override
# ---------------------------------------------------------------------------


def test_personas_dir_from_repo_true_applies_repo_personas_dir(tmp_path: Path) -> None:
    """When personas_dir_from_repo=True and repo has personas_dir, config picks it up."""
    personas = tmp_path / "my-personas"
    personas.mkdir()
    repo_yaml = tmp_path / ".agent-fleet.yaml"
    repo_yaml.write_text(f"personas_dir: {personas}\n", encoding="utf-8")

    ctx, err = build_fleet_context(_opts(workspace_arg=str(tmp_path), personas_dir_from_repo=True))
    assert err is None
    assert ctx is not None
    assert ctx.config.personas_dir == personas


def test_personas_dir_from_repo_false_does_not_apply(tmp_path: Path) -> None:
    """When personas_dir_from_repo=False, repo personas_dir does NOT override config."""
    personas = tmp_path / "repo-personas"
    personas.mkdir()
    repo_yaml = tmp_path / ".agent-fleet.yaml"
    repo_yaml.write_text(f"personas_dir: {personas}\n", encoding="utf-8")

    # Load a fleet config whose personas_dir we can inspect
    from agent_fleet.config import load_fleet_config as _lfc

    fleet_personas = _lfc().personas_dir

    ctx, err = build_fleet_context(_opts(workspace_arg=str(tmp_path), personas_dir_from_repo=False))
    assert err is None
    assert ctx is not None
    assert ctx.config.personas_dir == fleet_personas


# ---------------------------------------------------------------------------
# use_env_target_config: False → find_repo_config; True → resolve_repo_config
# ---------------------------------------------------------------------------


def test_use_env_target_config_false_ignores_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With use_env_target_config=False, AGENT_FLEET_TARGET_CONFIG env var is ignored."""
    other = tmp_path / "other"
    other.mkdir()
    other_yaml = other / ".agent-fleet.yaml"
    other_yaml.write_text("default_persona: phantom\n", encoding="utf-8")

    monkeypatch.setenv("AGENT_FLEET_TARGET_CONFIG", str(other_yaml))

    # workspace does NOT contain a .agent-fleet.yaml
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    ctx, err = build_fleet_context(_opts(workspace_arg=str(workspace), use_env_target_config=False))
    assert err is None
    assert ctx is not None
    # repo must be None (no .agent-fleet.yaml in workspace) — env var ignored
    assert ctx.repo is None


def test_use_env_target_config_true_honours_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With use_env_target_config=True, AGENT_FLEET_TARGET_CONFIG overrides search."""
    other = tmp_path / "other"
    other.mkdir()
    other_yaml = other / ".agent-fleet.yaml"
    other_yaml.write_text("default_persona: phantom\n", encoding="utf-8")

    monkeypatch.setenv("AGENT_FLEET_TARGET_CONFIG", str(other_yaml))

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    ctx, err = build_fleet_context(_opts(workspace_arg=str(workspace), use_env_target_config=True))
    assert err is None
    assert ctx is not None
    assert ctx.repo is not None
    assert ctx.repo.default_persona == "phantom"


# ---------------------------------------------------------------------------
# FleetContext fields
# ---------------------------------------------------------------------------


def test_fleet_context_has_expected_fields(tmp_path: Path) -> None:
    ctx, err = build_fleet_context(_opts(workspace_arg=str(tmp_path)))
    assert err is None
    assert ctx is not None
    assert isinstance(ctx.workspace, Path)
    assert ctx.config is not None
    # repo may be None if no .agent-fleet.yaml found
    assert hasattr(ctx, "repo")
    assert hasattr(ctx, "persona")
