"""Docs discoverability — no orphaned top-level doc.

A doc that no entry point links to is invisible: #97 (``docs/GATE.md``) and
#98 (``docs/FLEET-OPS.md``) both shipped real operator surfaces and neither was
reachable from the README, so nobody could find out how to run them.

This asserts every top-level file in ``docs/`` is referenced from ``README.md``
or ``docs/QUICKSTART.md``. The next orphaned doc fails here instead of being
discovered by a user.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
QUICKSTART = DOCS / "QUICKSTART.md"
README = ROOT / "README.md"

# Versioned specs, decision trails and stretch reports are historical records,
# not operator entry points, so they are exempt from the link requirement.
_SKIP_PREFIXES = ("v0.",)
_SKIP_DIRS = ("adr/", "superpowers/")


def _is_versioned_or_archivish(path: Path) -> bool:
    rel = path.relative_to(DOCS).as_posix()
    if rel.startswith(_SKIP_DIRS):
        return True
    return path.name.startswith(_SKIP_PREFIXES)


def _top_level_docs() -> list[Path]:
    return sorted(p for p in DOCS.iterdir() if p.is_file() and not _is_versioned_or_archivish(p))


def _entry_points() -> str:
    return f"{README.read_text(encoding='utf-8')}\n{QUICKSTART.read_text(encoding='utf-8')}"


def test_every_top_level_doc_is_linked() -> None:
    """Every top-level docs/ file must be referenced from README or QUICKSTART."""
    corpus = _entry_points()
    orphans = [p.name for p in _top_level_docs() if not re.search(re.escape(p.name), corpus)]
    assert not orphans, (
        "These docs/ top-level files are not referenced from README.md or "
        f"docs/QUICKSTART.md: {orphans}. Link them, or if the file is a "
        "historical record, move it under docs/adr/ or name it v0.* so the "
        "exemption applies."
    )


def test_gate_and_fleet_ops_are_discoverable() -> None:
    """The two docs this lane added must be reachable from the README table."""
    readme = README.read_text(encoding="utf-8")
    for doc in ("docs/GATE.md", "docs/FLEET-OPS.md"):
        assert doc in readme, f"{doc} must be linked from README.md"


def test_versioned_specs_are_skipped_by_the_orphan_check() -> None:
    """The exemption must actually exempt, and must not over-exempt."""
    names = {p.name for p in _top_level_docs()}
    assert not any(n.startswith(_SKIP_PREFIXES) for n in names), (
        "versioned specs must be filtered out before the orphan check"
    )
    # RELEASE.md is a live contributor doc, not a historical record, so the
    # exemption must not cover it.
    assert "RELEASE.md" in names, "RELEASE.md is linked from README; keep it checked"
