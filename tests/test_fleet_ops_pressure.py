"""CPU PSI reading — the throttle that replaced ``os.getloadavg``.

Two properties matter more than the parsing itself:

* it reads ``some``, never ``full`` (throttling on ``full`` is far too strict —
  it reads high while the machine is merely busy);
* it **fails open**. The incident this replaces was a *false* block that stalled
  every launch for forty minutes, so an unreadable file must never become
  another one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_fleet.fleet_ops import pressure

# A real cpu.pressure line, verbatim from this host.
SAMPLE = """some avg10=41.89 avg60=40.94 avg300=42.01 total=2725330824
full avg10=31.79 avg60=28.86 avg300=27.22 total=1636954102
"""


def test_some_avg10_is_parsed() -> None:
    assert pressure.read_some_avg10(_write(SAMPLE)) == pytest.approx(41.89)


def test_full_is_never_substituted_for_some(tmp_path: Path) -> None:
    """``full`` is the wrong number to throttle on; it must not leak in."""
    path = _write(SAMPLE, tmp_path)
    assert pressure.read_some_avg10(path) != pytest.approx(31.79)
    assert pressure.read_full_avg10(path) == pytest.approx(31.79)


def test_a_missing_file_yields_none(tmp_path: Path) -> None:
    assert pressure.read_some_avg10(tmp_path / "nope") is None


def test_a_file_with_no_some_line_yields_none(tmp_path: Path) -> None:
    assert pressure.read_some_avg10(_write("garbage\nnope\n", tmp_path)) is None


def test_a_directory_instead_of_a_file_yields_none(tmp_path: Path) -> None:
    assert pressure.read_some_avg10(tmp_path) is None


def test_throttle_prefers_the_configured_path(tmp_path: Path) -> None:
    good = _write("some avg10=7.50 avg60=0.0 avg300=0.0 total=1\n", tmp_path)
    reading = pressure.read_throttle(good, fallbacks=())
    assert reading.available is True
    assert reading.some_avg10 == pytest.approx(7.50)
    assert reading.path == good


def test_throttle_falls_back_in_order(tmp_path: Path) -> None:
    first = tmp_path / "first"  # does not exist
    second = _write("some avg10=3.25 avg60=0.0 avg300=0.0 total=1\n", tmp_path / "d")
    reading = pressure.read_throttle(first, fallbacks=(second,))
    assert reading.path == second
    assert reading.some_avg10 == pytest.approx(3.25)


def test_an_unavailable_reading_fails_open(tmp_path: Path) -> None:
    """The core guarantee: no PSI file means no throttling, ever."""
    reading = pressure.read_throttle(tmp_path / "nope", fallbacks=())
    assert reading.available is False
    assert reading.some_avg10 is None
    assert reading.saturated is False
    assert pressure.throttled(reading) is False


def test_throttling_is_strictly_above_the_ceiling() -> None:
    at_limit = pressure.Throttle(some_avg10=25.0, path=Path("/f"), available=True)
    over = pressure.Throttle(some_avg10=25.01, path=Path("/f"), available=True)
    under = pressure.Throttle(some_avg10=0.0, path=Path("/f"), available=True)
    assert pressure.throttled(at_limit, avg10_max=25.0) is False
    assert pressure.throttled(over, avg10_max=25.0) is True
    assert pressure.throttled(under, avg10_max=25.0) is False


def test_the_default_signal_is_the_agents_slice() -> None:
    """The number must describe the swarm, not the whole machine."""
    assert pressure.DEFAULT_PSI_PATH.name == "cpu.pressure"
    assert "agents.slice" in str(pressure.DEFAULT_PSI_PATH)
    assert pressure.FALLBACK_PATHS, "there must be somewhere to fall back to"


def test_to_dict_is_json_safe() -> None:
    import json

    reading = pressure.read_throttle(pressure.DEFAULT_PSI_PATH, fallbacks=())
    assert json.loads(json.dumps(reading.to_dict()))["available"] == reading.available


def test_the_module_never_calls_load_average() -> None:
    """AST-level: naming it in prose is fine, calling it is not."""
    import ast

    source = Path(pressure.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            assert name != "getloadavg"


def test_read_throttle_never_raises_on_a_garbage_path(tmp_path: Path) -> None:
    bad = _write("this is not psi at all", tmp_path)
    reading = pressure.read_throttle(bad, fallbacks=())
    assert reading.available is False


def _write(text: str, tmp_path: Path | None = None) -> Path:
    import tempfile

    directory = tmp_path if tmp_path is not None else Path(tempfile.mkdtemp())
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "cpu.pressure"
    path.write_text(text, encoding="utf-8")
    return path
