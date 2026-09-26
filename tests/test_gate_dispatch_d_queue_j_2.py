"""j-2: the documented default PSI signal (the agents slice) is never read.

``pressure.DEFAULT_PSI_PATH`` points at
``.../user@1000.service/agents.slice/cpu.pressure`` and the module docstring
calls the agents slice "the number that actually describes the swarm", because
that is the cgroup every lane, gate and dispatch child lives in. But it is *not*
in ``FALLBACK_PATHS``, and it is referenced nowhere outside its own definition.

So with the documented default configuration (no ``fleet_ops.dispatch.psi_path``
set, which is the default), ``run_dispatch``'s ``read_throttle()`` reads a
different cgroup than the one the swarm actually runs in — the user slice, which
carries every non-agent process. That produces both false blocks (unrelated load
throttles the swarm) and false negatives (a saturated agents slice leaves the
user slice under the ceiling).

This test asserts the invariant the claim names: when ``read_throttle`` is given
no explicit path, ``DEFAULT_PSI_PATH`` must be among the candidate files it
consults. It is written structurally (by recording which paths ``read_throttle``
asks about) so it does not depend on live cgroup values. At the current head this
fails: ``DEFAULT_PSI_PATH`` is never a candidate.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from agent_fleet.fleet_ops import pressure

if TYPE_CHECKING:
    import pytest


def test_read_throttle_without_a_path_consults_the_default_psi_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``read_throttle()`` (no path) must read the agents-slice default.

    The default throttle signal the module documents is ``DEFAULT_PSI_PATH``.
    When no explicit path is configured — the default — ``read_throttle`` must
    consult it. At the current head it consults only ``FALLBACK_PATHS``, so
    ``DEFAULT_PSI_PATH`` is never read and the dispatcher throttles on the wrong
    cgroup.
    """
    consulted: list[Path] = []

    def _record(path: Path | str) -> float | None:
        consulted.append(Path(path))
        return None  # force fallthrough; we only care which paths are consulted

    monkeypatch.setattr(pressure, "read_some_avg10", _record)

    pressure.read_throttle()  # no explicit path — the documented default config

    assert pressure.DEFAULT_PSI_PATH in consulted, (
        "read_throttle() with no configured path never consults DEFAULT_PSI_PATH "
        f"({pressure.DEFAULT_PSI_PATH}); it only consults {consulted}. The "
        "documented default signal (the agents slice) is therefore never read."
    )


def test_default_reading_ignores_the_agents_slice_and_reads_the_user_slice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default configuration must not report load the swarm is not causing.

    Simulate a host where the *user* slice is saturated (an unrelated service is
    busy) but the *agents* slice — where every lane/gate/dispatch child actually
    lives — is idle. With the documented default (no ``psi_path`` configured) the
    dispatcher must read the agents slice and therefore launch. At the current
    head the default configuration never consults the agents slice, so it reads
    the saturated user slice and falsely throttles the swarm.
    """
    busy_user = pressure.FALLBACK_PATHS[0]  # .../user@1000.service/cpu.pressure

    def _values(path: Path | str) -> float | None:
        p = Path(path)
        if p == pressure.DEFAULT_PSI_PATH:
            return 0.0  # agents slice idle: the swarm is not contended
        if p == busy_user:
            return 90.0  # user slice saturated: an unrelated service is busy
        return None

    monkeypatch.setattr(pressure, "read_some_avg10", _values)

    reading = pressure.read_throttle()  # no configured psi_path: the default

    assert not pressure.throttled(reading), (
        f"the default configuration was throttled by {reading.path} "
        f"(some avg10={reading.some_avg10}) even though the agents slice "
        f"({pressure.DEFAULT_PSI_PATH}) is idle. The swarm is being throttled by "
        "load on a cgroup none of its children live in."
    )
