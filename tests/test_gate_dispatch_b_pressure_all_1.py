"""``read_throttle()`` with no arguments must measure the swarm, not its parent.

The module documents ``DEFAULT_PSI_PATH`` (an ``agents.slice`` cpu.pressure file)
as the signal that "actually describes the swarm", and ``read_throttle()`` is
the only reader of that constant. But the candidate list it walks is
``[explicit path] + fallbacks``, and the shipped ``FALLBACK_PATHS`` contains only
the *containing* slices -- ``user@1000.service``, ``user.slice`` and the root
cgroup. ``DEFAULT_PSI_PATH`` is never a candidate, so the first fallback
(``user@1000.service/cpu.pressure``) always wins on a host where it is readable.

That is a semantically different measurement, not a noisy copy of the same one:
``user@1000.service`` aggregates ``app.slice`` and every other service running as
uid 1000, so a non-agent workload that saturates the box inflates the admission
throttle while the agents sit idle. On the host this was written for, the two
files read 0.04 and 4.47 respectively -- an order of magnitude apart.

These tests build a throwaway cgroup tree in tmp_path and monkeypatch the
constants, so they exercise the real ``read_throttle`` code path without reading
anything out of ``/sys``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agent_fleet.fleet_ops import pressure

if TYPE_CHECKING:
    from pathlib import Path

# The swarm is idle; every enclosing slice is pinned at 100% by unrelated work.
AGENTS_SLICE = (
    "some avg10=1.50 avg60=2.00 avg300=3.00 total=2725330824\n"
    "full avg10=0.10 avg60=0.20 avg300=0.30 total=1636954102\n"
)
APP_SERVICE = (
    "some avg10=99.00 avg60=98.00 avg300=97.00 total=3407096806\n"
    "full avg10=88.00 avg60=87.00 avg300=86.00 total=908172345\n"
)
USER_SLICE = (
    "some avg10=97.00 avg60=96.00 avg300=95.00 total=3089624650\n"
    "full avg10=80.00 avg60=79.00 avg300=78.00 total=1620100000\n"
)
ROOT_CGROUP = (
    "some avg10=95.00 avg60=94.00 avg300=93.00 total=3223816534\n"
    "full avg10=70.00 avg60=69.00 avg60=68.00 total=1000000000\n"
)


@pytest.fixture
def cgroup_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake cgroup v2 tree mirroring this host's layout, wired into the module."""
    service = tmp_path / "user.slice" / "user-1000.slice" / "user@1000.service"
    agents_slice = service / "agents.slice" / "cpu.pressure"
    _write(agents_slice, AGENTS_SLICE)
    _write(service / "cpu.pressure", APP_SERVICE)
    _write(tmp_path / "user.slice" / "cpu.pressure", USER_SLICE)
    _write(tmp_path / "cpu.pressure", ROOT_CGROUP)

    monkeypatch.setattr(pressure, "DEFAULT_PSI_PATH", agents_slice)
    # ``fallbacks`` is a keyword-only parameter whose default is bound at def
    # time, so re-pointing the module attribute would not reach it. Rebind the
    # parameter default itself, keeping the shipped three-fallback shape.
    monkeypatch.setitem(
        pressure.read_throttle.__kwdefaults__,
        "fallbacks",
        (
            service / "cpu.pressure",
            tmp_path / "user.slice" / "cpu.pressure",
            tmp_path / "cpu.pressure",
        ),
    )
    return agents_slice


def test_read_throttle_with_no_arguments_reads_the_agents_slice(
    cgroup_tree: Path,
) -> None:
    """The no-argument call is the one admission uses, and it must not fall back.

    Both the documented agents.slice file and its parent are readable here, so
    whichever candidate list is walked decides the answer.
    """
    reading = pressure.read_throttle()

    assert reading.available is True
    assert reading.path == cgroup_tree, (
        "read_throttle() never offered DEFAULT_PSI_PATH (agents.slice) as a "
        f"candidate and fell through to the parent service slice {reading.path}"
    )
    assert reading.some_avg10 == pytest.approx(1.50)


def test_non_fleet_cpu_pressure_does_not_block_fleet_launches(
    cgroup_tree: Path,
) -> None:
    """The practical harm: an unrelated app must not throttle the swarm.

    ``app.slice`` is pinned at 99.00, so the parent service slice reads
    saturated while the agents themselves are idle at 1.50. Gating launches on
    that reading would park a completely idle fleet.
    """
    reading = pressure.read_throttle()

    assert reading.path == cgroup_tree
    assert reading.some_avg10 is not None and reading.some_avg10 < pressure.DEFAULT_PSI_AVG10_MAX
    assert pressure.throttled(reading) is False, (
        f"admission blocked on non-fleet pressure: {reading.path} read "
        f"{reading.some_avg10}, but the agents slice read 1.50"
    )


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
