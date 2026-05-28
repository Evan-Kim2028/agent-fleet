"""Tests for the load_persona_md function with loadout_size support."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from agent_fleet.personas import load_persona_md

if TYPE_CHECKING:
    import pytest

ROOT = Path(__file__).resolve().parent.parent
PERSONAS_DIR = ROOT / "agent_fleet" / "personas"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_new_layout(tmp_path: Path, *, with_reference: bool = True) -> Path:
    """Create a minimal new-layout persona directory under tmp_path."""
    persona_dir = tmp_path / "mypersona"
    persona_dir.mkdir()
    (persona_dir / "loadout.md").write_text("## Role\n\nTest persona loadout.", encoding="utf-8")
    if with_reference:
        ref_dir = persona_dir / "reference"
        ref_dir.mkdir()
        (ref_dir / "INDEX.md").write_text(
            "# Reference index\n\n| File | Contents |\n|------|----------|\n"
            "| `tips.md` | Useful tips |",
            encoding="utf-8",
        )
        # First paragraph is the heading line; second paragraph is the body text.
        (ref_dir / "tips.md").write_text(
            "First paragraph content.\n\nSecond paragraph here.",
            encoding="utf-8",
        )
    return tmp_path


def _make_legacy_persona(tmp_path: Path) -> Path:
    """Create a flat legacy persona .md file under tmp_path."""
    (tmp_path / "legacypersona.md").write_text(
        "## Legacy\n\nLegacy flat persona content.", encoding="utf-8"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# New-layout: minimal
# ---------------------------------------------------------------------------


def test_new_layout_minimal_returns_loadout_only(tmp_path: Path) -> None:
    personas_dir = _make_new_layout(tmp_path)
    result = load_persona_md("mypersona", personas_dir=personas_dir, loadout_size="minimal")
    assert result.strip() == "## Role\n\nTest persona loadout."
    assert "INDEX" not in result
    assert "tips" not in result.lower()


# ---------------------------------------------------------------------------
# New-layout: standard
# ---------------------------------------------------------------------------


def test_new_layout_standard_returns_loadout_and_index(tmp_path: Path) -> None:
    personas_dir = _make_new_layout(tmp_path)
    result = load_persona_md("mypersona", personas_dir=personas_dir, loadout_size="standard")
    assert "## Role" in result
    assert "Test persona loadout" in result
    assert "Reference index" in result
    assert "tips.md" in result
    # tips body should NOT be included at standard
    assert "First paragraph content" not in result


def test_new_layout_standard_delimiter_present(tmp_path: Path) -> None:
    personas_dir = _make_new_layout(tmp_path)
    result = load_persona_md("mypersona", personas_dir=personas_dir, loadout_size="standard")
    assert "---" in result, "expected delimiter between loadout and index"


def test_new_layout_standard_loadout_before_index(tmp_path: Path) -> None:
    personas_dir = _make_new_layout(tmp_path)
    result = load_persona_md("mypersona", personas_dir=personas_dir, loadout_size="standard")
    loadout_pos = result.index("Test persona loadout")
    index_pos = result.index("Reference index")
    assert loadout_pos < index_pos, "loadout.md must appear before INDEX.md"


# ---------------------------------------------------------------------------
# New-layout: full
# ---------------------------------------------------------------------------


def test_new_layout_full_returns_loadout_index_and_first_paras(tmp_path: Path) -> None:
    personas_dir = _make_new_layout(tmp_path)
    result = load_persona_md("mypersona", personas_dir=personas_dir, loadout_size="full")
    assert "## Role" in result
    assert "Reference index" in result
    assert "First paragraph content" in result


def test_new_layout_full_excludes_second_paragraph(tmp_path: Path) -> None:
    personas_dir = _make_new_layout(tmp_path)
    result = load_persona_md("mypersona", personas_dir=personas_dir, loadout_size="full")
    assert "Second paragraph here" not in result


def test_new_layout_full_skips_index_in_reference_expansion(tmp_path: Path) -> None:
    """INDEX.md itself must not appear in the expanded reference excerpts."""
    personas_dir = _make_new_layout(tmp_path)
    result = load_persona_md("mypersona", personas_dir=personas_dir, loadout_size="full")
    # INDEX.md content appears once (as the index section), not duplicated as an excerpt
    assert result.count("Reference index") == 1


# ---------------------------------------------------------------------------
# Legacy flat persona
# ---------------------------------------------------------------------------


def test_legacy_any_size_returns_flat_content(tmp_path: Path) -> None:
    personas_dir = _make_legacy_persona(tmp_path)
    for size in ("minimal", "standard", "full"):
        result = load_persona_md(
            "legacypersona",
            personas_dir=personas_dir,
            loadout_size=size,  # type: ignore[arg-type]
        )
        assert "Legacy flat persona content" in result


def test_legacy_minimal_no_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    personas_dir = _make_legacy_persona(tmp_path)
    with caplog.at_level(logging.WARNING, logger="agent_fleet.personas"):
        load_persona_md("legacypersona", personas_dir=personas_dir, loadout_size="minimal")
    assert not any("legacy" in r.message for r in caplog.records)


def test_legacy_standard_logs_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    personas_dir = _make_legacy_persona(tmp_path)
    with caplog.at_level(logging.WARNING, logger="agent_fleet.personas"):
        load_persona_md("legacypersona", personas_dir=personas_dir, loadout_size="standard")
    assert any("legacy" in r.message for r in caplog.records)


def test_legacy_full_logs_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    personas_dir = _make_legacy_persona(tmp_path)
    with caplog.at_level(logging.WARNING, logger="agent_fleet.personas"):
        load_persona_md("legacypersona", personas_dir=personas_dir, loadout_size="full")
    assert any("legacy" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Lakestore migrated persona
# ---------------------------------------------------------------------------


def test_lakestore_minimal_loads_without_error() -> None:
    result = load_persona_md("lakestore", personas_dir=PERSONAS_DIR, loadout_size="minimal")
    assert result.strip()
    assert "Lakestore" in result or "lakestore" in result.lower()


def test_lakestore_standard_loads_without_error() -> None:
    result = load_persona_md("lakestore", personas_dir=PERSONAS_DIR, loadout_size="standard")
    assert result.strip()
    assert "INDEX" in result or "index" in result.lower() or "Reference" in result


def test_lakestore_full_loads_without_error() -> None:
    result = load_persona_md("lakestore", personas_dir=PERSONAS_DIR, loadout_size="full")
    assert result.strip()
    # full should include at least one reference excerpt
    assert "excerpt" in result or len(result) > len(
        load_persona_md("lakestore", personas_dir=PERSONAS_DIR, loadout_size="standard")
    )


def test_lakestore_standard_larger_than_minimal() -> None:
    minimal = load_persona_md("lakestore", personas_dir=PERSONAS_DIR, loadout_size="minimal")
    standard = load_persona_md("lakestore", personas_dir=PERSONAS_DIR, loadout_size="standard")
    assert len(standard) > len(minimal)


def test_lakestore_full_larger_than_standard() -> None:
    standard = load_persona_md("lakestore", personas_dir=PERSONAS_DIR, loadout_size="standard")
    full = load_persona_md("lakestore", personas_dir=PERSONAS_DIR, loadout_size="full")
    assert len(full) > len(standard)
