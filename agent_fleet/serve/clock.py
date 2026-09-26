"""Injectable clock — the only source of time inside :mod:`agent_fleet.serve`.

Every timeout in this package is expressed against :class:`Clock`, never against
``time.time()`` directly. Three reasons, in order of how much pain they caused
in the bash drivers this replaces:

``time.time()`` moves backwards. An NTP correction mid-run turns a 20-minute
stage timeout into a negative age, and a stuck agent survives the watchdog for
another NTP correction. :meth:`Clock.monotonic` cannot go backwards.

Timers need to be testable without sleeping. A watchdog whose only clock is
``time.sleep`` cannot be tested for a 1-hour orphan rule in less than an hour.

The two are not interchangeable. Ages against a *file's* mtime or a *lock's*
acquisition epoch must use wall clock, because the file was stamped by another
process; the supervisor's own backoff must use monotonic. :class:`Clock`
exposes both and the callers pick deliberately.
"""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    """Time source for the supervisor, the watchdog and the capacity loop."""

    def monotonic(self) -> float:
        """Seconds from an arbitrary origin; never decreases."""

    def time(self) -> float:
        """Wall-clock epoch seconds, comparable to a file's mtime."""

    def sleep(self, seconds: float) -> None:
        """Block for *seconds*."""


class SystemClock:
    """The real clock."""

    def monotonic(self) -> float:
        return time.monotonic()

    def time(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class FakeClock:
    """A clock tests drive by hand.

    ``sleep`` advances the clock instead of blocking, so a test can assert on
    a multi-hour backoff schedule or a 1-hour orphan window in microseconds.
    """

    def __init__(self, *, start_monotonic: float = 0.0, start_time: float = 1_000_000.0) -> None:
        self._mono = start_monotonic
        self._wall = start_time
        #: Every ``sleep`` call, in order, so a test can assert the backoff
        #: schedule rather than just its endpoint.
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self._mono

    def time(self) -> float:
        return self._wall

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        """Move both clocks forward by *seconds* without recording a sleep."""
        if seconds > 0:
            self._mono += seconds
            self._wall += seconds

    def set_wall(self, epoch: float) -> None:
        """Set wall clock only — for tests that stamp files with another epoch."""
        self._wall = epoch


__all__ = ["Clock", "FakeClock", "SystemClock"]
