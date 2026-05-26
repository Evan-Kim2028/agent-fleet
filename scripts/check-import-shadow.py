#!/usr/bin/env python3
"""Detect agent_fleet namespace shadowing from stray checkout directories.

A checkout at ~/Documents/agent_fleet (underscore) is a common footgun: if that
directory is on sys.path (cwd, PYTHONPATH, or editable-install confusion), Python
imports the wrong tree and the CLI behaves unpredictably.

This script reports the active import location, scans sys.path for shadow
entries, and warns when known risky directories exist on disk. It never deletes
or modifies user directories.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PACKAGE = "agent_fleet"

# Checkouts here commonly shadow the installed package when cwd or PYTHONPATH
# includes the directory. See docs/FLEET-CONFIG.md#import-shadow.
KNOWN_SHADOW_ROOTS: tuple[Path, ...] = (Path.home() / "Documents" / "agent_fleet",)


def _resolve(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _import_root(module_file: str | None) -> Path | None:
    if not module_file:
        return None
    return _resolve(module_file).parent


def _repo_root(package_dir: Path) -> Path:
    return package_dir.parent


def _path_entries() -> list[Path]:
    entries: list[Path] = []
    for raw in sys.path:
        if not raw:
            continue
        try:
            entries.append(_resolve(raw))
        except OSError:
            continue
    return entries


def _shadow_roots_on_syspath(*, exclude: Path | None) -> list[Path]:
    shadows: list[Path] = []
    for entry in _path_entries():
        pkg = entry / PACKAGE
        if not pkg.is_dir():
            continue
        if exclude is not None and entry == exclude:
            continue
        shadows.append(entry)
    return shadows


def _known_shadows_on_disk() -> list[Path]:
    return [root for root in KNOWN_SHADOW_ROOTS if root.is_dir() and (root / PACKAGE).is_dir()]


def _is_under(path: Path, ancestor: Path) -> bool:
    try:
        path.relative_to(ancestor)
    except ValueError:
        return False
    return True


def check_import_shadow(*, strict_disk: bool = False) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) describing shadow state."""
    import agent_fleet

    errors: list[str] = []
    warnings: list[str] = []

    active_pkg = _import_root(agent_fleet.__file__)
    active_repo = _repo_root(active_pkg) if active_pkg else None

    if active_pkg is not None:
        print(f"agent_fleet imported from: {active_pkg}")
    else:
        errors.append("Could not resolve agent_fleet.__file__")

    for shadow_root in _known_shadows_on_disk():
        if active_repo is not None and _is_under(active_repo, shadow_root):
            errors.append(
                f"Active import is under known shadow checkout: {shadow_root}\n"
                "  Use a hyphenated clone path (~/agent-fleet-dev) or pip install -e ."
            )
        elif strict_disk:
            warnings.append(
                f"Known shadow checkout exists on disk: {shadow_root}\n"
                "  Avoid running Python with this directory as cwd or on PYTHONPATH."
            )

    syspath_shadows = _shadow_roots_on_syspath(exclude=active_repo)
    for entry in syspath_shadows:
        if any(_is_under(entry, known) or entry == known for known in KNOWN_SHADOW_ROOTS):
            errors.append(f"sys.path exposes shadow package root: {entry}")
        elif active_repo is not None and entry != active_repo:
            warnings.append(f"sys.path contains alternate agent_fleet root: {entry}")

    return errors, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict-disk",
        action="store_true",
        help="Warn when known shadow checkout directories exist even if not active",
    )
    args = parser.parse_args(argv)

    errors, warnings = check_import_shadow(strict_disk=args.strict_disk)

    for msg in warnings:
        print(f"warning: {msg}", file=sys.stderr)

    if errors:
        for msg in errors:
            print(f"error: {msg}", file=sys.stderr)
        print(
            "\nRemediation: clone to ~/agent-fleet-dev (hyphen), pip install -e ., "
            "and do not add ~/Documents/agent_fleet to PYTHONPATH.\n"
            "See docs/FLEET-CONFIG.md#import-shadow",
            file=sys.stderr,
        )
        return 1

    if warnings:
        return 0

    print("No agent_fleet import shadow detected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
