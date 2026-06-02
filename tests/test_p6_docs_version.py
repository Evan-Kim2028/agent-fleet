"""P6 — Docs hard-update + version 0.11.2.

Tests cover:
- pyproject.toml and agent_fleet/__init__.py are both at 0.11.2.
- README.md uses ``fleet`` (not ``agent-fleet``) for every user-facing command example.
- docs/QUICKSTART.md uses ``fleet`` command surface.
- docs/NEW-REPO.md uses ``fleet`` command surface.
- docs/FLEET-CONFIG.md uses ``fleet`` command surface.
- docs/SCHEDULES.md uses ``fleet schedule`` (not ``agent-fleet-schedule``).
- docs/PERSONAS.md uses ``fleet`` command surface.
- CHANGELOG.md exists and mentions 0.11.2.
- No doc file contains a migration table (old-name → new-name).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


# ---------------------------------------------------------------------------
# Version consistency
# ---------------------------------------------------------------------------


def test_pyproject_version_is_0_11_2() -> None:
    content = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "0.11.2"' in content, "pyproject.toml version must be 0.11.2"


def test_init_version_is_0_11_2() -> None:
    content = (ROOT / "agent_fleet" / "__init__.py").read_text(encoding="utf-8")
    assert '__version__ = "0.11.2"' in content, "agent_fleet/__init__.py __version__ must be 0.11.2"


def test_versions_match() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    init_py = (ROOT / "agent_fleet" / "__init__.py").read_text(encoding="utf-8")

    pyproject_match = re.search(r'version = "([^"]+)"', pyproject)
    init_match = re.search(r'__version__ = "([^"]+)"', init_py)

    assert pyproject_match and init_match
    assert pyproject_match.group(1) == init_match.group(1), (
        f"pyproject version {pyproject_match.group(1)!r} != "
        f"__init__ version {init_match.group(1)!r}"
    )


# ---------------------------------------------------------------------------
# CHANGELOG
# ---------------------------------------------------------------------------


def test_changelog_exists() -> None:
    assert (ROOT / "CHANGELOG.md").exists(), "CHANGELOG.md must exist after P6"


def test_changelog_mentions_0_11_2() -> None:
    content = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "0.11.2" in content, "CHANGELOG.md must mention version 0.11.2"


# ---------------------------------------------------------------------------
# README uses fleet surface
# ---------------------------------------------------------------------------


def _readme() -> str:
    return (ROOT / "README.md").read_text(encoding="utf-8")


def test_readme_uses_fleet_run() -> None:
    readme = _readme()
    assert "fleet run" in readme, "README must use 'fleet run'"


def test_readme_uses_fleet_doctor_or_summon() -> None:
    readme = _readme()
    assert "fleet doctor" in readme or "fleet summon" in readme, (
        "README must use 'fleet doctor' or 'fleet summon'"
    )


def test_readme_uses_fleet_loop() -> None:
    readme = _readme()
    assert "fleet loop" in readme, "README must use 'fleet loop' (not agent-fleet-pr-loop)"


def test_readme_no_agent_fleet_run_command() -> None:
    """Old 'agent-fleet run' invocations must not appear in README."""
    readme = _readme()
    # Allow agent-fleet as a package reference (e.g. git+https://...agent-fleet.git)
    # but not as a CLI invocation: 'agent-fleet run', 'agent-fleet doctor', etc.
    _cmds = r"(run|doctor|personas|review|loop|watch|schedule|dispatch|summon|init)"
    bad_cli_pattern = re.compile(rf"^\s*(uv run )?agent-fleet {_cmds}\b", re.MULTILINE)
    matches = bad_cli_pattern.findall(readme)
    assert not matches, f"README still contains old 'agent-fleet <cmd>' CLI invocations: {matches}"


# ---------------------------------------------------------------------------
# QUICKSTART uses fleet surface
# ---------------------------------------------------------------------------


def _quickstart() -> str:
    return (DOCS / "QUICKSTART.md").read_text(encoding="utf-8")


def test_quickstart_uses_fleet_run() -> None:
    assert "fleet run" in _quickstart()


def test_quickstart_uses_fleet_personas() -> None:
    assert "fleet personas" in _quickstart()


def test_quickstart_uses_fleet_summon() -> None:
    assert "fleet summon" in _quickstart()


def test_quickstart_no_old_agent_fleet_commands() -> None:
    qs = _quickstart()
    _cmds = r"(run|doctor|personas|review|loop|watch|schedule|dispatch|summon|init)"
    bad = re.compile(rf"^\s*(uv run )?agent-fleet {_cmds}\b", re.MULTILINE)
    matches = bad.findall(qs)
    assert not matches, f"QUICKSTART still has old CLI invocations: {matches}"


# ---------------------------------------------------------------------------
# NEW-REPO uses fleet surface
# ---------------------------------------------------------------------------


def _new_repo() -> str:
    return (DOCS / "NEW-REPO.md").read_text(encoding="utf-8")


def test_new_repo_uses_fleet_init() -> None:
    assert "fleet init" in _new_repo()


def test_new_repo_uses_fleet_loop() -> None:
    assert "fleet loop" in _new_repo()


def test_new_repo_uses_fleet_run() -> None:
    assert "fleet run" in _new_repo()


def test_new_repo_no_old_agent_fleet_commands() -> None:
    nr = _new_repo()
    _cmds = r"(run|doctor|personas|review|loop|watch|schedule|dispatch|summon|init)"
    bad = re.compile(rf"^\s*(uv run )?agent-fleet {_cmds}\b", re.MULTILINE)
    matches = bad.findall(nr)
    assert not matches, f"NEW-REPO still has old CLI invocations: {matches}"


# ---------------------------------------------------------------------------
# SCHEDULES uses fleet schedule
# ---------------------------------------------------------------------------


def _schedules() -> str:
    return (DOCS / "SCHEDULES.md").read_text(encoding="utf-8")


def test_schedules_uses_fleet_schedule() -> None:
    assert "fleet schedule" in _schedules()


def test_schedules_no_agent_fleet_schedule_command() -> None:
    sched = _schedules()
    # agent-fleet-schedule is the shim name (undocumented), must not appear
    assert "agent-fleet-schedule" not in sched, (
        "SCHEDULES.md must not document 'agent-fleet-schedule'; use 'fleet schedule'"
    )


# ---------------------------------------------------------------------------
# FLEET-CONFIG uses fleet surface
# ---------------------------------------------------------------------------


def _fleet_config() -> str:
    return (DOCS / "FLEET-CONFIG.md").read_text(encoding="utf-8")


def test_fleet_config_uses_fleet_run_config() -> None:
    fc = _fleet_config()
    # Should show 'fleet run "..." --config ...' or similar
    assert "fleet run" in fc or "fleet doctor" in fc or "fleet runs" in fc, (
        "FLEET-CONFIG must use fleet CLI commands"
    )


def test_fleet_config_no_old_agent_fleet_commands() -> None:
    fc = _fleet_config()
    _cmds = r"(run|doctor|personas|review|loop|watch|schedule|dispatch|summon|init|paths)"
    bad = re.compile(rf"^\s*(uv run )?agent-fleet {_cmds}\b", re.MULTILINE)
    matches = bad.findall(fc)
    assert not matches, f"FLEET-CONFIG still has old CLI invocations: {matches}"


# ---------------------------------------------------------------------------
# PERSONAS uses fleet surface
# ---------------------------------------------------------------------------


def _personas() -> str:
    return (DOCS / "PERSONAS.md").read_text(encoding="utf-8")


def test_personas_uses_fleet_init() -> None:
    assert "fleet init" in _personas()


def test_personas_uses_fleet_run() -> None:
    assert "fleet run" in _personas()


def test_personas_uses_fleet_personas() -> None:
    assert "fleet personas" in _personas()


# ---------------------------------------------------------------------------
# No migration table in any doc
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "doc",
    [
        "README.md",
        "docs/QUICKSTART.md",
        "docs/NEW-REPO.md",
        "docs/FLEET-CONFIG.md",
        "docs/PERSONAS.md",
        "docs/SCHEDULES.md",
    ],
)
def test_no_migration_table(doc: str) -> None:
    """Docs must not contain old→new migration tables (per locked decision)."""
    content = (ROOT / doc).read_text(encoding="utf-8")
    # Migration tables have explicit headers pairing old and new command names.
    # Match only table header rows like "| Old command | New command |" or
    # "| Old name | New name |" — not incidental prose uses of "old" or "new".
    bad_pattern = re.compile(
        r"^\|?\s*(Old (command|name))\s*\|.*\|\s*(New (command|name))",
        re.IGNORECASE | re.MULTILINE,
    )
    matches = bad_pattern.findall(content)
    assert not matches, f"{doc} appears to contain a migration table: {matches[:3]}"


# ---------------------------------------------------------------------------
# ADR exists
# ---------------------------------------------------------------------------


def test_adr_0001_exists() -> None:
    adr = DOCS / "adr" / "0001-disable-argparse-abbreviation.md"
    assert adr.exists(), "docs/adr/0001-disable-argparse-abbreviation.md must exist"


def test_adr_0001_mentions_allow_abbrev() -> None:
    adr = DOCS / "adr" / "0001-disable-argparse-abbreviation.md"
    content = adr.read_text(encoding="utf-8")
    assert "allow_abbrev" in content, "ADR must mention allow_abbrev"
