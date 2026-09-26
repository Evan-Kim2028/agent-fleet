"""CPU pressure (PSI) as a throttle signal.

The dispatcher's load signal used to be ``os.getloadavg()``, and that was
wrong for this machine for a specific reason: this box runs its agents inside a
cgroup with a **CPU quota**, so a task that is quota-throttled stays in the
run queue and is counted as *running* by the load average. Load therefore reads
high when the machine is doing nothing useful, and the dispatcher sat on its
hands for forty minutes refusing to launch anything.

PSI measures the right thing instead. ``some avg10`` is the percentage of the
last ten seconds in which at least one task was runnable but had to wait for
CPU — saturation, not accounting. ``full`` is the stricter "every non-idle task
was waiting", which is what you want to *avoid* rather than to throttle at.

Reading PSI is therefore fail-**open**: a missing, unreadable, or unparseable
file yields ``available=False`` and never blocks a launch. The incident this
replaces was a false block; a missing file must not become another one.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: The agents slice is where every lane, gate, and dispatch child lives, so its
#: CPU pressure is the number that actually describes the swarm. Verified
#: present on this host; overridable via ``fleet_ops.dispatch.psi_path``.
DEFAULT_PSI_PATH = Path(
    "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/agents.slice/cpu.pressure"
)

#: Tried in order when the configured path is unusable. The user's own slice is
#: a much closer proxy than the root cgroup, so it is the first fallback.
FALLBACK_PATHS: tuple[Path, ...] = (
    Path("/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/cpu.pressure"),
    Path("/sys/fs/cgroup/user.slice/cpu.pressure"),
    Path("/sys/fs/cgroup/cpu.pressure"),
)

#: ``some avg10=41.89 avg60=40.94 avg300=42.01 total=2725330824``
_SOME_RE = re.compile(r"^some\s+avg10=([0-9]+(?:\.[0-9]+)?)\b", re.MULTILINE)
_FULL_RE = re.compile(r"^full\s+avg10=([0-9]+(?:\.[0-9]+)?)\b", re.MULTILINE)

#: Above this ``some avg10`` the machine is contended enough that launching more
#: agents makes everything slower. Deliberately a real saturation signal: the
#: observed agents-slice value under a full swarm was ~42, and the box stayed
#: productive there, so the default sits well below the *saturated* end rather
#: than wherever loadavg happened to point on the day.
DEFAULT_PSI_AVG10_MAX = 25.0


@dataclass(frozen=True)
class Throttle:
    """One CPU-pressure reading.

    ``available`` is False when no readable PSI file was found; ``some_avg10``
    is then None and callers must proceed unthrottled.
    """

    some_avg10: float | None
    path: Path | None
    available: bool

    @property
    def saturated(self) -> bool:
        """Whether this reading is *above the ceiling*, not merely readable.

        Delegates to :func:`throttled` so the two never disagree. An idle host
        (``some avg10=0.00``) is available but not saturated, and an unavailable
        reading is never saturated.
        """
        return throttled(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "some_avg10": self.some_avg10,
            "path": str(self.path) if self.path else None,
            "available": self.available,
        }


def _parse_some_avg10(text: str) -> float | None:
    match = _SOME_RE.search(text)
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:  # pragma: no cover - the regex only matches digits
        return None


def read_some_avg10(path: Path | str) -> float | None:
    """Parse the ``some avg10`` value out of one ``cpu.pressure`` file.

    Returns None when the file is missing, unreadable, or carries no ``some``
    line. A ``full`` value is never substituted: throttling on ``full`` is far
    too strict (it reads high while the machine is merely busy).
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return _parse_some_avg10(text)


def read_full_avg10(path: Path | str) -> float | None:
    """Parse ``full avg10`` — exposed for diagnostics, not used to throttle."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _FULL_RE.search(text)
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:  # pragma: no cover - the regex only matches digits
        return None


def read_throttle(
    path: Path | str | None = None,
    *,
    fallbacks: tuple[Path, ...] = FALLBACK_PATHS,
) -> Throttle:
    """Read the first usable PSI file from *path* (or the default) then *fallbacks*.

    With no *path* the default is ``DEFAULT_PSI_PATH``, the agents slice, and it
    must be consulted before the containing slices: ``user@1000.service`` is the
    parent of ``app.slice`` and every other service running as uid 1000, so
    leading with it throttles the swarm on unrelated CPU load. The default is
    resolved on each call rather than captured in the signature, so the
    ``fleet_ops.dispatch.psi_path`` override is picked up.

    Never raises. The result is ``available=False`` when nothing readable was
    found, which callers treat as "do not throttle".
    """
    candidates: list[Path] = [Path(path) if path is not None else Path(DEFAULT_PSI_PATH)]
    candidates.extend(Path(p) for p in fallbacks)
    for candidate in candidates:
        value = read_some_avg10(candidate)
        if value is not None:
            return Throttle(some_avg10=value, path=candidate, available=True)
    logger.debug("no readable cpu.pressure; dispatch will not throttle on CPU pressure")
    return Throttle(some_avg10=None, path=None, available=False)


def throttled(throttle: Throttle, *, avg10_max: float = DEFAULT_PSI_AVG10_MAX) -> bool:
    """Whether *throttle* says the machine is too contended to launch more.

    Fails open: an unavailable reading never blocks.
    """
    if not throttle.available or throttle.some_avg10 is None:
        return False
    return throttle.some_avg10 > avg10_max
