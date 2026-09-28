#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

GIB = 1024**3


@dataclass(frozen=True)
class CapacityDecision:
    admitted: bool
    host_available_bytes: int | None
    fleet_headroom_bytes: int | None
    reason: str


def decide_capacity(
    host_available_bytes: int | None,
    fleet_current_bytes: int | None,
    fleet_max_bytes: int | None,
    *,
    host_reserve_bytes: int = 16 * GIB,
    fleet_headroom_bytes: int = 2 * GIB,
) -> CapacityDecision:
    if host_available_bytes is None or fleet_current_bytes is None or fleet_max_bytes is None:
        return CapacityDecision(False, host_available_bytes, None, "capacity_unknown")
    remaining = max(0, fleet_max_bytes - fleet_current_bytes)
    if host_available_bytes < host_reserve_bytes:
        return CapacityDecision(False, host_available_bytes, remaining, "host_reserve")
    if remaining < fleet_headroom_bytes:
        return CapacityDecision(False, host_available_bytes, remaining, "fleet_headroom")
    return CapacityDecision(True, host_available_bytes, remaining, "capacity_available")


def read_mem_available(path: Path = Path("/proc/meminfo")) -> int | None:
    try:
        for line in path.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except OSError:
        return None
    except ValueError:
        return None
    return None


def read_cgroup_value(path: Path) -> int | None:
    try:
        value = path.read_text().strip()
        return None if value == "max" else int(value)
    except OSError:
        return None
    except ValueError:
        return None


def evaluate(
    fleet_cgroup: Path,
    *,
    meminfo: Path = Path("/proc/meminfo"),
    host_reserve_bytes: int = 16 * GIB,
    minimum_fleet_headroom_bytes: int = 2 * GIB,
) -> CapacityDecision:
    return decide_capacity(
        read_mem_available(meminfo),
        read_cgroup_value(fleet_cgroup / "memory.current"),
        read_cgroup_value(fleet_cgroup / "memory.max"),
        host_reserve_bytes=host_reserve_bytes,
        fleet_headroom_bytes=minimum_fleet_headroom_bytes,
    )


def default_fleet_cgroup() -> Path:
    uid = os.getuid()
    return Path(f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/fleet.slice")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fleet-cgroup", type=Path, default=default_fleet_cgroup())
    parser.add_argument("--meminfo", type=Path, default=Path("/proc/meminfo"))
    parser.add_argument("--state", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--host-reserve-gib", type=int, default=16)
    parser.add_argument("--fleet-headroom-gib", type=int, default=2)
    args = parser.parse_args()
    decision = evaluate(
        args.fleet_cgroup,
        meminfo=args.meminfo,
        host_reserve_bytes=args.host_reserve_gib * GIB,
        minimum_fleet_headroom_bytes=args.fleet_headroom_gib * GIB,
    )
    if args.state:
        args.state.parent.mkdir(parents=True, exist_ok=True)
        temp = args.state.with_name(f"{args.state.name}.{os.getpid()}.tmp")
        temp.write_text("open\n" if decision.admitted else "closed\n")
        temp.replace(args.state)
    if args.report:
        print(json.dumps(asdict(decision), sort_keys=True))
    if args.check:
        return 0 if decision.admitted else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
