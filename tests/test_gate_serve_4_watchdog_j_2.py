"""A real two-way block must be expressible, or rule (d) is dead code.

``flock`` is the authoritative mutual exclusion: it is real, it blocks, and it
is released even by SIGKILL. What flock cannot do is say *who* is waiting on
*what*, so :mod:`agent_fleet.serve.locks` keeps a JSON record beside it. That
record is the entire substrate of watchdog rule (d) — the module's own docstring
calls it out:

    * **deadlock** — a ``waiting`` record naming a lock another component
      ``holds``, where that holder is in turn ``waiting`` for a lock this one
      holds. The cycle is explicit in the data, not inferred from timing.

The cycle is only "explicit in the data" if ``waiting_for`` is populated. The
only writer of a ``waiting`` record in the whole package is ``mark_waiting``,
called from exactly one place: the failure branch of :meth:`LockRegistry.hold`,
which hardcodes ``waiting_for=None``. ``hold`` takes no parameter through which
a caller could supply an edge, so the one code path real components use can
never record one, and ``deadlocks()`` — which skips any record without
``waiting_for`` — has nothing to walk.

The four shipped deadlock tests do not notice, because all four call
``mark_waiting(...)`` directly with a hand-written ``waiting_for`` rather than
driving the acquisition path a component actually takes. The tests below drive
``hold()`` and check that a block the kernel is genuinely enforcing right now
can be reported by ``deadlocks()`` and acted on by the watchdog.
"""

from __future__ import annotations

import inspect
import os
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.config import ServeConfig, WatchdogConfig
from agent_fleet.serve.locks import STATE_WAITING, LockRegistry
from agent_fleet.serve.supervisor import Supervisor
from agent_fleet.serve.watchdog import RULE_DEADLOCK, Watchdog

if TYPE_CHECKING:
    from collections.abc import Iterator

#: The threshold the detector is asked to use, in minutes. Every acquisition
#: below is stamped an hour in the past, so any cycle that exists is far past it.
DEADLOCK_MINUTES = 1
ONE_HOUR = 3600.0
NOW = 1_000_000.0


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


def _config() -> ServeConfig:
    """Rule (d) at a threshold the two-way block below is well past."""
    return ServeConfig(operator="op", watchdog=WatchdogConfig(deadlock_minutes=DEADLOCK_MINUTES))


@contextmanager
def _holds(registry: LockRegistry, name: str, *, holder: str) -> Iterator[None]:
    """Take the real flock for *name* and keep it for the block's duration.

    Each ``hold()`` opens a **new** file descriptor, which is how a second
    component reaches a lock. That distinction is load-bearing rather than
    stylistic: ``flock(2)`` conflicts are per open-file-description, and a second
    ``flock`` on the *same* descriptor is a no-op that silently succeeds. Taking
    the same lock twice through one descriptor would never contend, and these
    tests would pass without the kernel ever enforcing anything.
    """
    with registry.hold(name, holder=holder) as acquired:
        assert acquired is True, f"precondition: nobody else holds {name!r}"
        yield


def _failed(registry: LockRegistry, name: str, *, holder: str) -> None:
    """Attempt a contended lock and assert it really was refused."""
    with registry.hold(name, holder=holder, now=NOW - ONE_HOUR) as acquired:
        assert acquired is False, f"precondition: {name!r} really was contended"


def _build_two_way_block() -> None:
    """A symmetric two-way block, built only through the public acquisition API.

    merger holds ``merge`` and blocks on ``dispatch``; dispatcher holds
    ``dispatch`` and blocks on ``merge``. Both attempts are an hour old, so the
    cycle is far past ``deadlock_minutes``. The kernel is enforcing both halves
    of this while the calls run.

    The two ``hold()`` blocks that remain open afterwards are deliberate: their
    ``finally`` clause writes the record back to ``free``, so a block that has
    already unwound leaves nothing to detect. A deadlock is a condition that
    persists, and this keeps the real thing alive while the detector looks at it.
    """
    merger = LockRegistry("op")
    dispatcher = LockRegistry("op")
    with _holds(merger, "merge", holder="merger"):
        with _holds(dispatcher, "dispatch", holder="dispatcher"):
            _failed(merger, "dispatch", holder="merger")
            _failed(dispatcher, "merge", holder="dispatcher")


def _records(registry: LockRegistry) -> dict[str, tuple[str, str, str | None]]:
    return {
        name: (record.state, record.holder, record.waiting_for)
        for name, record in registry.all_records().items()
    }


def test_a_failed_acquisition_records_the_lock_it_is_waiting_for() -> None:
    """``hold()``'s one job on failure is to record the edge that blocks it.

    Its docstring is explicit: "Yields False when the lock is already held,
    having first written a ``waiting`` record **naming it**." Naming it means
    ``waiting_for`` is the lock that could not be had — precisely the field the
    deadlock walk refuses to continue without.
    """
    registry = LockRegistry("op")
    with _holds(registry, "merge", holder="merger"):
        _failed(registry, "merge", holder="dispatcher")

        record = registry.read("merge")
        assert record is not None
        assert record.state == STATE_WAITING
        assert record.holder == "dispatcher"
        # This is the edge the detector needs and hold() has no way to express.
        assert record.waiting_for == "merge", (
            f"hold() wrote a waiting record with waiting_for={record.waiting_for!r}. "
            f"The block the kernel is enforcing right now cannot be written down by "
            f"the only code path that produces waiting records, so "
            f"LockRegistry.deadlocks() has no edge to follow."
        )


def test_a_real_two_way_block_is_detected_as_a_deadlock() -> None:
    """Two components each blocked on the other, built only through ``hold()``."""
    registry = LockRegistry("op")
    _build_two_way_block()

    cycles = registry.deadlocks(now=NOW, threshold_minutes=DEADLOCK_MINUTES)
    assert cycles, (
        "a two-way block an hour old — merger holds 'merge' and is blocked on "
        "'dispatch', dispatcher holds 'dispatch' and is blocked on 'merge' — is a "
        "textbook deadlock that the kernel is enforcing right now, but "
        f"deadlocks() returned nothing. Records as written: {_records(registry)}. "
        "Every waiting record carries waiting_for=None, so the walk skips all of "
        "them at its first condition."
    )
    assert {r.name for r in cycles[0]} == {"merge", "dispatch"}


def test_the_watchdog_releases_a_real_deadlock() -> None:
    """Rule (d) must fire on a deadlock built the way components build one."""
    registry = LockRegistry("op")
    _build_two_way_block()

    config = _config()
    sup = Supervisor("op", config, clock=FakeClock())
    try:
        watchdog = Watchdog("op", config, sup, clock=FakeClock(), locks=registry)
        report = watchdog.tick()
        assert RULE_DEADLOCK in report.by_rule(), (
            f"the watchdog saw no deadlock in a two-way block an hour old. "
            f"remediations={report.remediations} "
            f"records={_records(registry)}"
        )
    finally:
        with suppress(Exception):
            sup.shutdown()


def test_hold_offers_no_way_to_declare_the_edge() -> None:
    """The signature is the final word: there is no parameter to fill it in.

    This is the cheapest way to see the defect without running a process, and it
    is the part a fix has to change: giving ``hold`` a ``waiting_for`` keyword is
    what would make the edge expressible at the one place records are written.
    """
    params = inspect.signature(LockRegistry.hold).parameters
    assert "waiting_for" in params, (
        f"LockRegistry.hold accepts {sorted(params)}, none of which can carry the "
        f"lock a failed acquisition is waiting for. mark_waiting takes the field, "
        f"hold does not, so the only real acquisition path can never record a "
        f"deadlock edge and rule (d) is unreachable from production code."
    )


def test_hold_fails_while_a_foreign_component_holds_the_lock() -> None:
    """Precondition: the two components really do contend, in this process too.

    Same-process contention is real because every ``hold()`` opens a fresh file
    description. If this ever stopped holding, the tests above would be proving
    nothing about the kernel's behaviour — only about a simulated record.
    """
    registry = LockRegistry("op")
    with _holds(registry, "dispatch", holder="dispatcher"):
        _failed(registry, "dispatch", holder="merger")
        record = registry.read("dispatch")
        assert record is not None and record.state == STATE_WAITING
        assert record.holder == "merger"
        assert os.getpid() == record.pid
