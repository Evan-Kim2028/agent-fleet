"""``serve_dir`` must confine every operator to its own slot under ``serve/``.

``fleet_ops.fleet_ops.worktree.sanitize_component`` -- the sanitizer the
``fleet serve`` module docstring names as the reason operator scoping matters --
does ``.strip("-.")`` and falls back to ``"lane"``, so ``..`` and ``.`` can never
survive into a path. ``agent_fleet.serve.paths.serve_dir`` instead keeps ``.`` in
its allow-list and never strips it, so a traversal name walks straight back up
out of ``serve/`` into the shared ``$AGENT_FLEET_HOME``.

The blast radius is not theoretical: ``ensure_serve_dir("..")`` returns the fleet
home itself and creates ``components/`` and ``locks/`` there as siblings of
``lanes/`` and ``journal/``, and ``state_path("..")`` resolves to
``<home>/state.json`` -- a file no operator ever intended to write, sitting next
to shared fleet data. That is the opposite of the no-clobber guarantee the
module docstring advertises.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_fleet.serve.paths import (
    children_path,
    ensure_serve_dir,
    events_path,
    locks_dir,
    serve_dir,
    state_path,
    write_json_atomic,
)

#: Siblings that live directly under the shared fleet home. If a serve operator
#: ever creates its files as siblings of these, it has escaped the serve root.
SHARED_HOME_SIBLINGS = ("lanes", "slots", "level_up", "journal")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated ``$AGENT_FLEET_HOME`` holding the shared fleet layout."""
    root = tmp_path / "agent-fleet-home"
    for name in SHARED_HOME_SIBLINGS:
        (root / name).mkdir(parents=True)
    monkeypatch.setenv("AGENT_FLEET_HOME", str(root))
    return root


def _real(path: Path) -> Path:
    return Path(os.path.realpath(path))


def test_serve_dir_dotdot_escapes_the_serve_root(home: Path) -> None:
    """``serve_dir("..")`` must stay inside ``serve/``; today it resolves to the fleet home."""
    resolved = _real(serve_dir(".."))

    assert resolved != _real(home), (
        f"serve_dir('..') resolved to the shared fleet home {resolved} -- it "
        "escapes the per-operator serve/ directory entirely"
    )
    assert resolved.is_relative_to(_real(home) / "serve"), (
        f"serve_dir('..') resolved to {resolved}, which is outside {_real(home) / 'serve'}"
    )


def test_ensure_serve_dir_dotdot_does_not_create_state_in_the_shared_home(
    home: Path,
) -> None:
    """The escaped directory must not be created next to the shared fleet data."""
    created = _real(ensure_serve_dir(".."))

    assert not (home / "components").exists(), (
        "ensure_serve_dir('..') created components/ as a sibling of the shared "
        f"fleet data in {home}"
    )
    assert not (home / "locks").exists(), (
        f"ensure_serve_dir('..') created locks/ as a sibling of the shared fleet data in {home}"
    )
    assert not (home / "state.json").exists()
    assert created.is_relative_to(_real(home) / "serve")


def test_writing_state_for_dotdot_does_not_clobber_the_shared_home(home: Path) -> None:
    """``state_path("..")`` must not alias ``<home>/state.json``."""
    write_json_atomic(state_path(".."), {"last_tick": 1})

    assert not (home / "state.json").exists(), (
        "writing serve state for operator '..' wrote to <home>/state.json, "
        "clobbering a path in the shared fleet home rather than a serve operator's slot"
    )
    assert (home / "children.jsonl").exists() is False
    assert (home / "events.jsonl").exists() is False


def test_dot_operator_state_is_not_stored_under_the_shared_serve_parent(home: Path) -> None:
    """A bare ``.`` operator must still get its own named directory."""
    write_json_atomic(state_path("."), {"last_tick": 1})

    assert (home / "serve" / "state.json").exists() is False, (
        "operator '.' wrote state.json directly into the serve/ parent, where a "
        "future 'serve' subdirectory or shared operator slot would collide with it"
    )
    assert state_path(".").is_relative_to(_real(home) / "serve")
    assert _real(state_path(".")) != _real(state_path("alpha")), (
        "operator '.' must not share its state file with the named operator 'alpha'"
    )


def test_serve_paths_for_every_traversal_name_stay_under_serve(home: Path) -> None:
    """Every path helper must resolve inside ``serve/`` for traversal-ish names."""
    serve_root = _real(home / "serve")
    for operator in (".", "..", "...", "./..", "a/../..", "-", "_"):
        for builder in (
            state_path,
            children_path,
            events_path,
            serve_dir,
            locks_dir,
        ):
            resolved = _real(builder(operator))
            assert resolved.is_relative_to(serve_root), (
                f"{builder.__name__}({operator!r}) resolved to {resolved}, "
                f"outside the serve root {serve_root}"
            )
