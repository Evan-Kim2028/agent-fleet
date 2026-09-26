"""``Throttle.saturated`` must report *saturation*, not merely *readability*.

The property is public API on a frozen dataclass, so whatever it returns is what
a caller gets when it asks "is this host under CPU pressure?" Today it is
``self.available and self.some_avg10 is not None`` -- an exact restatement of
``available``, carrying no information about the reading it wraps. Every
successful read is therefore "saturated", including a machine whose
``some avg10`` is ``0.00``, i.e. one that spent none of the last ten seconds
with a runnable task waiting for CPU.

That is the opposite of the word's meaning everywhere else in the module. The
``DEFAULT_PSI_AVG10_MAX`` docstring puts the ceiling "well below the *saturated*
end", and :func:`throttled` is the one place that tests it, with
``some_avg10 > avg10_max``. An operator reading ``saturated`` off a status
surface, or a dispatcher gating launches on it, would park an idle fleet on an
idle host -- the precise false-block failure this module was written to fix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_fleet.fleet_ops import pressure

if TYPE_CHECKING:
    from pathlib import Path


def _read(tmp_path: Path, value: str) -> pressure.Throttle:
    """A real ``Throttle`` built through the real parse path, no PSI file needed."""
    path = tmp_path / f"cpu.pressure.{value}"
    path.write_text(
        f"some avg10={value} avg60=0.00 avg300=0.00 total=1\n"
        "full avg10=0.00 avg60=0.00 avg300=0.00 total=1\n",
        encoding="utf-8",
    )
    return pressure.read_throttle(path, fallbacks=())


def test_an_idle_machine_is_not_saturated(tmp_path: Path) -> None:
    """``some avg10=0.00`` means no task waited for CPU at all: not saturated."""
    idle = _read(tmp_path, "0.00")

    assert idle.available is True, "the read succeeded, so this exercises the live path"
    assert idle.some_avg10 == 0.0
    assert pressure.throttled(idle) is False, "the module's own ceiling says not throttled"
    assert idle.saturated is False, (
        "a completely idle host (some avg10=0.00) reported as saturated; "
        "saturated is a pure alias for available and never looks at the reading"
    )


def test_saturated_discriminates_between_an_idle_and_a_hot_host(tmp_path: Path) -> None:
    """Two available readings must not collapse to the same verdict."""
    idle = _read(tmp_path, "0.00")
    hot = _read(tmp_path, "99.00")

    assert idle.saturated is not hot.saturated, (
        "available readings of 0.00 and 99.00 are indistinguishable through "
        "saturated, so the property conveys no pressure information at all"
    )
    assert hot.saturated is True, "a host at some avg10=99.00 is genuinely saturated"


def test_saturated_agrees_with_the_ceiling_below_it(tmp_path: Path) -> None:
    """``saturated`` must track the same ceiling :func:`throttled` enforces."""
    under = _read(tmp_path, "1.50")
    assert pressure.throttled(under) is False
    assert under.saturated is False, (
        "1.50 is far below DEFAULT_PSI_AVG10_MAX, so the reading is not saturated"
    )
