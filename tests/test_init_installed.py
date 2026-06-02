"""Tests that fleet init/summon work when installed (no access to top-level examples/).

This exercises the case where the package is installed as a uv tool and the
repo-level examples/ directory is not available — only the installed wheel
contents are present.

The tests monkeypatch Path.read_text to raise FileNotFoundError for the old
examples/ path, simulating the installed case.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from collections.abc import Callable


def _make_examples_path_raiser(
    cli_module_file: str,
) -> tuple[Path, Callable[[Path, str | None, str | None, str | None], str]]:
    """Return (old_examples_path, patched_read_text) to simulate installed env."""

    old_examples_path = (
        Path(cli_module_file).resolve().parent.parent / "examples" / "repo.agent-fleet.yaml"
    )
    _original_read_text = Path.read_text

    def _patched_read_text(
        self: Path,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> str:
        if self.resolve() == old_examples_path.resolve():
            raise FileNotFoundError(f"simulated missing installed path: {self}")
        return _original_read_text(self, encoding=encoding, errors=errors, newline=newline)

    return old_examples_path, _patched_read_text


def test_cmd_init_does_not_depend_on_toplevel_examples(tmp_path: Path) -> None:
    """Verify cmd_init reads from package resources, not the repo examples/ dir.

    Core regression test: even if the path that the old code used
    (parent.parent/examples/repo.agent-fleet.yaml) raises FileNotFoundError,
    cmd_init must still succeed because it now loads the template via
    importlib.resources.
    """
    import agent_fleet.cli as cli_module
    from agent_fleet.cli import cmd_init

    _, _patched_read_text = _make_examples_path_raiser(cli_module.__file__ or "")

    dest = tmp_path / ".agent-fleet.yaml"
    init_args = argparse.Namespace(path=str(tmp_path), force=False)

    with patch.object(Path, "read_text", _patched_read_text):
        rc = cmd_init(init_args)

    assert rc == 0, f"cmd_init failed (rc={rc}) — it still depends on the un-packaged examples/ dir"
    assert dest.exists(), ".agent-fleet.yaml was not created"
    content = dest.read_text(encoding="utf-8")
    assert "name:" in content, "generated config missing 'name:' field"
    assert "default_branch:" in content, "generated config missing 'default_branch:' field"
    assert "test_command:" in content, "generated config missing 'test_command:' field"


def test_cmd_init_generates_valid_config(tmp_path: Path) -> None:
    """cmd_init must produce a valid .agent-fleet.yaml with expected fields."""
    from agent_fleet.cli import cmd_init

    dest = tmp_path / ".agent-fleet.yaml"
    init_args = argparse.Namespace(path=str(tmp_path), force=False)

    rc = cmd_init(init_args)

    assert rc == 0, f"cmd_init returned {rc}"
    assert dest.exists(), ".agent-fleet.yaml was not created"

    content = dest.read_text(encoding="utf-8")
    assert "name:" in content, "generated config missing 'name:' field"
    assert "default_branch:" in content, "generated config missing 'default_branch:' field"
    assert "test_command:" in content, "generated config missing 'test_command:' field"


def test_cmd_summon_creates_config_without_examples_dir(tmp_path: Path) -> None:
    """cmd_summon must work end-to-end in an installed environment."""
    import agent_fleet.cli as cli_module
    from agent_fleet.cli import cmd_summon

    _, _patched_read_text = _make_examples_path_raiser(cli_module.__file__ or "")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    dest = workspace / ".agent-fleet.yaml"

    with patch.object(Path, "read_text", _patched_read_text):
        summon_args = argparse.Namespace(workspace=str(workspace), config=None)
        cmd_summon(summon_args)

    # rc may be non-zero due to missing API keys in the doctor step, but the
    # config file must have been created successfully.
    assert dest.exists(), ".agent-fleet.yaml was not created by fleet summon"
    content = dest.read_text(encoding="utf-8")
    assert "name:" in content
    assert "default_branch:" in content


def test_cmd_init_force_overwrites_existing(tmp_path: Path) -> None:
    """cmd_init --force must overwrite an existing .agent-fleet.yaml."""
    from agent_fleet.cli import cmd_init

    dest = tmp_path / ".agent-fleet.yaml"
    dest.write_text("old content", encoding="utf-8")

    init_args = argparse.Namespace(path=str(tmp_path), force=True)
    rc = cmd_init(init_args)

    assert rc == 0
    content = dest.read_text(encoding="utf-8")
    assert content != "old content", "force flag should overwrite"
    assert "name:" in content


def test_cmd_init_refuses_overwrite_without_force(tmp_path: Path) -> None:
    """cmd_init without --force must refuse to overwrite an existing config."""
    from agent_fleet.cli import cmd_init

    dest = tmp_path / ".agent-fleet.yaml"
    dest.write_text("old content", encoding="utf-8")

    init_args = argparse.Namespace(path=str(tmp_path), force=False)
    rc = cmd_init(init_args)

    assert rc != 0
    assert dest.read_text(encoding="utf-8") == "old content"
