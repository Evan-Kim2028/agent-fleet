"""Pressure reading from the cgroup — and the refusal to guess when it fails.

The controller in :mod:`agent_fleet.serve.capacity` sizes the fleet. Its input
is pressure-stall information (PSI) and the memory ratio, read from one
directory in the cgroup hierarchy.

**Never the load average.** ``os.getloadavg()`` is the obvious thing to reach
for and it is wrong here for a specific, measurable reason: under a cgroup CPU
quota the run queue reflects tasks *throttled* by the quota, not work waiting
for a CPU. A 4-way quota on a 16-core box reports a load of 64 while every core
is 96% idle, because the tasks cannot be scheduled and are sitting in
``throttle_cfs``. Admitting more work in response makes it worse. PSI measures
the stall directly and does not have that failure mode, which is why this module
exists rather than a two-line loadavg call.

**A missing file is not an idle machine.** This is the failure mode that makes
a capacity controller dangerous, so it gets a first-class type rather than a
float. On the box this was written for, ``/sys/fs/cgroup/agents.slice`` does not
exist — systemd nests the user manager's slice, so the real path is
``/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/agents.slice``. A
reader that turned that into ``0.0`` would report "no pressure" and the AIMD
controller would ratchet lanes to its ceiling forever, on a saturated machine.
So :class:`PressureReading` carries ``source_ok`` separately from the numbers,
and the controller's response to ``source_ok is False`` is to fall to the hard
floor and say so out loud.

All filesystem access goes through *root* so tests can fabricate a whole cgroup
tree, including the failure shapes: missing directory, missing files,
``memory.max`` holding the string ``max``, and the cgroup v1 layout.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Where the cgroup filesystem is mounted (cgroup v2 unified hierarchy).
CGROUP_ROOT = Path("/sys/fs/cgroup")

#: The slice serve's children run in. A *name*, not a path: systemd nests it
#: under the caller's user slice, so ``/sys/fs/cgroup/<name>`` is wrong on every
#: systemd user-session box. :func:`resolve_cgroup` finds it.
DEFAULT_CGROUP_NAME = "agents.slice"

#: Roots searched, in order, when resolving a bare slice name. Bounded on
#: purpose: an unbounded walk of /sys is a hang waiting to happen on a busy box.
_SEARCH_ROOTS = (
    ".",
    "user.slice",
    f"user.slice/user-{os.getuid()}.slice",
    f"user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service",
)

#: ``max`` in memory.max means unlimited, and must not become a huge int.
_MEMORY_UNLIMITED = "max"


@dataclass(frozen=True)
class CpuPressure:
    """One line-pair of ``cpu.pressure``.

    ``some`` is time with at least one task stalled; ``full`` is time with *all*
    non-idle tasks stalled. ``avg60`` is the controller's signal, and ``total``
    is the monotonic counter that distinguishes "idle" from "unreadable": a real
    cgroup always has a total that grows from boot.
    """

    some_avg10: float = 0.0
    some_avg60: float = 0.0
    some_avg300: float = 0.0
    some_total_us: int = 0
    full_avg60: float = 0.0
    full_total_us: int = 0

    def to_dict(self) -> dict[str, float | int]:
        return {
            "some_avg10": self.some_avg10,
            "some_avg60": self.some_avg60,
            "some_avg300": self.some_avg300,
            "some_total_us": self.some_total_us,
            "full_avg60": self.full_avg60,
            "full_total_us": self.full_total_us,
        }


@dataclass(frozen=True)
class PressureReading:
    """One tick's view of the fleet's cgroup.

    ``source_ok`` is the field that matters. When it is False, every number in
    this object is meaningless and the controller must not reason from it — see
    the module docstring.
    """

    ok: bool = False
    error: str = ""
    path: str = ""
    hierarchy: str = "v2"
    cpu: CpuPressure = field(default_factory=CpuPressure)
    memory_used_bytes: int | None = None
    memory_max_bytes: int | None = None
    memory_ratio: float | None = None

    @property
    def source_ok(self) -> bool:
        """Alias for :attr:`ok`; named for how the controller reads it."""
        return self.ok

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "error": self.error,
            "path": self.path,
            "hierarchy": self.hierarchy,
            "cpu": self.cpu.to_dict(),
            "memory_used_bytes": self.memory_used_bytes,
            "memory_max_bytes": self.memory_max_bytes,
            "memory_ratio": self.memory_ratio,
        }


def _parse_psi(text: str) -> CpuPressure:
    """Parse a ``cpu.pressure``/``io.pressure`` document.

    Format is two lines, ``some avg10=N avg60=N avg300=N total=N`` then the
    same for ``full``. Unparseable input yields all-zero rather than raising:
    a malformed document is a broken source, and :attr:`PressureReading.ok` is
    where that fact gets recorded.
    """
    some: dict[str, float] = {}
    full: dict[str, float] = {}
    target: dict[str, float] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("some"):
            target = some
        elif line.startswith("full"):
            target = full
        else:
            continue
        for token in line.split()[1:]:
            key, _, value = token.partition("=")
            if value:
                target[key] = float(value)
    return CpuPressure(
        some_avg10=some.get("avg10", 0.0),
        some_avg60=some.get("avg60", 0.0),
        some_avg300=some.get("avg300", 0.0),
        some_total_us=int(some.get("total", 0)),
        full_avg60=full.get("avg60", 0.0),
        full_total_us=int(full.get("total", 0)),
    )


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except OSError, ValueError:
        return None


def _read_optional_psi(path: Path) -> CpuPressure | None:
    try:
        return _parse_psi(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def detect_hierarchy(root: Path) -> str:
    """``"v2"`` when the unified hierarchy is mounted, else ``"v1"``.

    v2 has ``cgroup.controllers`` at the mount root; v1 has per-controller
    subdirectories. The distinction decides which file names exist at all:
    v1 has no ``memory.current``/``memory.max`` (it is
    ``memory.usage_in_bytes``/``memory.limit_in_bytes``) and no per-cgroup
    ``*.pressure`` files in the general case.
    """
    if (root / "cgroup.controllers").exists():
        return "v2"
    if any((root / name).is_dir() for name in ("memory", "cpu,cpuacct", "cpuacct")):
        return "v1"
    return "unknown"


def resolve_cgroup(name_or_path: str, *, root: Path = CGROUP_ROOT) -> Path | None:
    """Resolve a slice name or path to an existing cgroup directory.

    A value containing ``/`` is treated as a path relative to *root* (or
    absolute) and used as-is. A bare name is searched under a bounded set of
    systemd-nesting prefixes — see :data:`_SEARCH_ROOTS`. Returns ``None`` when
    nothing matched, which the caller must treat as a failed read, never as an
    idle machine.
    """
    if not name_or_path:
        return None
    candidate = Path(name_or_path)
    if candidate.is_absolute():
        return candidate if candidate.is_dir() else None
    if "/" in name_or_path:
        direct = root / name_or_path
        return direct if direct.is_dir() else None
    for prefix in _SEARCH_ROOTS:
        base = root if prefix == "." else root / prefix
        found = base / name_or_path
        if found.is_dir():
            return found
    return None


def self_cgroup_path(*, proc_root: Path = Path("/proc")) -> str | None:
    """This process's own cgroup, from ``/proc/self/cgroup``.

    The most reliable default there is: whatever slice the supervisor is
    actually in is, by construction, where its children live. Returns the
    v2 unified-hierarchy path (the line whose controller list is empty).
    """
    try:
        text = (proc_root / "self" / "cgroup").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[1] == "":
            return parts[2] or None
    return None


def _v1_memory(cg: Path) -> tuple[int | None, int | None]:
    used = _read_int(cg / "memory.usage_in_bytes")
    limit = _read_int(cg / "memory.limit_in_bytes")
    # v1 spells "unlimited" as a sentinel near LONG_MAX rather than a keyword.
    if limit is not None and limit > (1 << 60):
        limit = None
    return used, limit


def read_pressure(
    cgroup: str = DEFAULT_CGROUP_NAME,
    *,
    root: Path = CGROUP_ROOT,
) -> PressureReading:
    """Read cpu/memory/io pressure and the memory ratio for one cgroup.

    ``ok`` is True only when at least the CPU pressure file was readable. A
    readable file whose ``total`` counter is zero is still a real reading (a
    cgroup that has existed for a microsecond), so the controller gets a
    genuine zero and increases capacity — that is correct. What it must never
    get is a zero manufactured by a missing file, and that is the distinction
    ``ok`` carries.
    """
    hierarchy = detect_hierarchy(root)
    resolved = resolve_cgroup(cgroup, root=root)
    if resolved is None:
        return PressureReading(
            ok=False,
            error=(
                f"cgroup {cgroup!r} not found under {root} "
                f"(tried: {', '.join(_SEARCH_ROOTS)}). Pressure unknown — "
                f"the capacity controller will hold at its floor, not assume the machine is idle."
            ),
            path="",
            hierarchy=hierarchy,
        )

    if hierarchy == "v1":
        used, limit = _v1_memory(resolved)
        ratio = (used / limit) if (used is not None and limit) else None
        return PressureReading(
            ok=False,
            error=(
                f"cgroup v1 hierarchy at {root}: no per-cgroup PSI files, so CPU pressure "
                f"cannot be read. Only the memory ratio is available."
            ),
            path=str(resolved),
            hierarchy="v1",
            memory_used_bytes=used,
            memory_max_bytes=limit,
            memory_ratio=ratio,
        )

    cpu = _read_optional_psi(resolved / "cpu.pressure")
    if cpu is None:
        return PressureReading(
            ok=False,
            error=(
                f"{resolved}/cpu.pressure is not readable. Pressure unknown — the capacity "
                f"controller will hold at its floor, not assume the machine is idle."
            ),
            path=str(resolved),
            hierarchy=hierarchy,
        )

    used = _read_int(resolved / "memory.current")
    limit_raw = _read_int(resolved / "memory.max")
    limit: int | None = None
    if limit_raw is None:
        try:
            text = (resolved / "memory.max").read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if text == _MEMORY_UNLIMITED:
            limit = None
    else:
        limit = limit_raw
    ratio = (used / limit) if (used is not None and limit) else None

    return PressureReading(
        ok=True,
        error="",
        path=str(resolved),
        hierarchy=hierarchy,
        cpu=cpu,
        memory_used_bytes=used,
        memory_max_bytes=limit,
        memory_ratio=ratio,
    )


def read_all_pressure(
    cgroup: str = DEFAULT_CGROUP_NAME,
    *,
    root: Path = CGROUP_ROOT,
) -> Mapping[str, PressureReading]:
    """Cpu + memory + io pressure in one call.

    ``io.pressure`` is optional: many kernels and most container runtimes do
    not expose it, and a missing io file must not invalidate a perfectly good
    cpu reading. It is reported as an unavailable sub-reading rather than
    silently dropped, so ``serve status`` can say which signals it had.
    """
    base = read_pressure(cgroup, root=root)
    result: dict[str, PressureReading] = {"cpu": base}
    if not base.ok or not base.path:
        result["io"] = PressureReading(ok=False, error="cpu reading unavailable")
        return result
    cg = Path(base.path)
    io_reading = _read_optional_psi(cg / "io.pressure")
    if io_reading is None:
        result["io"] = PressureReading(ok=False, error=f"{cg}/io.pressure is not readable")
    else:
        result["io"] = PressureReading(
            ok=True, path=str(cg), hierarchy=base.hierarchy, cpu=io_reading
        )
    return result


__all__ = [
    "CGROUP_ROOT",
    "DEFAULT_CGROUP_NAME",
    "CpuPressure",
    "PressureReading",
    "detect_hierarchy",
    "read_all_pressure",
    "read_pressure",
    "resolve_cgroup",
    "self_cgroup_path",
]
