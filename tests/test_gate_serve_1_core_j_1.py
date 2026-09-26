"""Component-name containment for ``agent_fleet.serve.paths``.

``serve_dir()`` runs the operator through an allow-list sanitizer before it
becomes a path segment, but ``component_log_path()`` / ``component_pid_path()``
interpolate the component ``name`` straight into an f-string. A component name
carrying path separators therefore escapes the operator's ``components/``
directory and lands in the shared ``$AGENT_FLEET_HOME`` -- a sibling of the
``lanes/`` and ``journal/`` directories.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.paths import (
    component_dir,
    component_log_path,
    component_pid_path,
    ensure_serve_dir,
    write_json_atomic,
)

if TYPE_CHECKING:
    from pathlib import Path

# Names that must never be able to steer the returned path out of components/.
TRAVERSING_NAMES = [
    "../../../pwned",
    "../../pwned",
    "../pwned",
    "sub/dir",
    "..",
    "../../../../../../../../tmp/pwned",
    "a/../../b",
    "./../../pwned",
    "....//....//pwned",
    "/abs/pwned",
]


def _is_inside(child: Path, parent: Path) -> bool:
    """True when *child* resolves to *parent* or something beneath it."""
    resolved_child = child.resolve()
    resolved_parent = parent.resolve()
    return resolved_child == resolved_parent or resolved_parent in resolved_child.parents


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point $AGENT_FLEET_HOME at a throwaway directory."""
    home = tmp_path / "fleet-home"
    home.mkdir()
    monkeypatch.setenv("AGENT_FLEET_HOME", str(home))
    return home


@pytest.mark.parametrize("name", TRAVERSING_NAMES)
def test_component_log_path_stays_inside_component_dir(name: str) -> None:
    ensure_serve_dir("alpha")
    root = component_dir("alpha")
    assert _is_inside(component_log_path("alpha", name), root), (
        f"component_log_path('alpha', {name!r}) escaped components/: "
        f"{component_log_path('alpha', name)} is not under {root}"
    )


@pytest.mark.parametrize("name", TRAVERSING_NAMES)
def test_component_pid_path_stays_inside_component_dir(name: str) -> None:
    ensure_serve_dir("alpha")
    root = component_dir("alpha")
    assert _is_inside(component_pid_path("alpha", name), root), (
        f"component_pid_path('alpha', {name!r}) escaped components/: "
        f"{component_pid_path('alpha', name)} is not under {root}"
    )


def test_traversing_component_name_cannot_write_into_fleet_home() -> None:
    """The reported repro: a write lands in $AGENT_FLEET_HOME, not components/."""
    home = ensure_serve_dir("alpha").parents[1]  # .../<home>/serve/alpha -> <home>
    assert home.name == "fleet-home"

    write_json_atomic(component_log_path("alpha", "../../../pwned"), {"x": 1})

    assert not (home / "pwned.log").exists(), (
        "traversing component name wrote pwned.log into the shared fleet home: "
        f"{sorted(p.name for p in home.iterdir())}"
    )
    # Whatever was written has to live under the operator's components/ dir.
    written = [p for p in home.rglob("*.log") if p.name == "pwned.log"]
    assert written, "expected the log write to land somewhere under the fleet home"
    for path in written:
        assert _is_inside(path, component_dir("alpha")), f"{path} escaped components/"


def test_traversing_component_name_cannot_write_pid_file_outside() -> None:
    """A traversed pid path is later read and signalled by a supervisor."""
    home = ensure_serve_dir("alpha").parents[1]
    assert home.name == "fleet-home"

    write_json_atomic(component_pid_path("alpha", "../../../pwned"), {"pid": 4242})

    assert not (home / "pwned.pid").exists(), (
        "traversing component name wrote pwned.pid into the shared fleet home: "
        f"{sorted(p.name for p in home.iterdir())}"
    )
    written = list(home.rglob("pwned.pid"))
    for path in written:
        assert _is_inside(path, component_dir("alpha")), f"{path} escaped components/"


def test_ordinary_component_names_are_unchanged() -> None:
    """Sanitizing must not disturb the normal names serve actually uses."""
    ensure_serve_dir("alpha")
    root = component_dir("alpha")
    assert component_log_path("alpha", "dispatch") == root / "dispatch.log"
    assert component_pid_path("alpha", "gate") == root / "gate.pid"

    # And a plain write still works end to end.
    target = component_log_path("alpha", "dispatch")
    write_json_atomic(target, {"pid": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"pid": 1}
