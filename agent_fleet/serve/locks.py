"""Lock records: who holds a lock, who wants it, and when.

The bash fleet serialized its merge work with ``flock -n 9 || exit 0`` — three
sites in ``automerge2.sh`` alone, one per repo plus one for the batch path. That
is correct and unobservable: the lock file has no content, so when a merge
stopped holding, nothing could say who had it, how long they had had it, or
whether anyone was still waiting. The deadlock had to be diagnosed by a human
reading a log.

So serve keeps the *lock* as an flock — the kernel does the mutual exclusion and
releases it even on SIGKILL, which is exactly the property that makes flock
right here — and keeps the *record* of the lock as a JSON file next to it, so
the watchdog has something to reason about. Two mechanisms, one logical lock:

    <serve>/locks/<name>.lock    the flock
    <serve>/locks/<name>.json    {"holder", "pid", "starttime", "acquired_epoch",
                                  "state", "waiting_for", "wanting"}

This is what makes watchdog rules (c) and (d) implementable at all:

* **stale lock** — a ``held`` record whose holder pid is dead, older than the
  configured grace. The flock is already free; the record is the stale part.
* **deadlock** — a ``waiting`` record naming a lock another component ``holds``,
  where that holder is in turn ``waiting`` for a lock this one holds. The cycle
  is explicit in the data, not inferred from timing.

A record is only ever advisory metadata beside the authoritative flock. If a
record is wrong the kernel still prevents double-holding; if a record is
missing, the lock is simply untracked, which is a reporting gap and never a
correctness one.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_fleet.serve.paths import exclusive_lock, locks_dir
from agent_fleet.serve.procs import ProcIdentity, pid_alive

if TYPE_CHECKING:
    from collections.abc import Iterator

STATE_FREE = "free"
STATE_HELD = "held"
STATE_WAITING = "waiting"


def record_path(directory: Path, name: str) -> Path:
    return directory / f"{name}.json"


def flock_path(directory: Path, name: str) -> Path:
    return directory / f"{name}.lock"


@dataclass
class LockRecord:
    """What serve knows about one lock."""

    name: str
    state: str = STATE_FREE
    holder: str = ""
    pid: int | None = None
    starttime: int | None = None
    acquired_epoch: float = 0.0
    #: The lock this holder is itself waiting for, when state is ``waiting``.
    #: This is the edge that makes deadlock detection a graph walk.
    waiting_for: str | None = None
    #: What the waiter intends to do once it gets the lock.
    wanting: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "state": self.state,
            "holder": self.holder,
            "pid": self.pid,
            "starttime": self.starttime,
            "acquired_epoch": self.acquired_epoch,
            "waiting_for": self.waiting_for,
            "wanting": self.wanting,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> LockRecord | None:
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            return None
        pid = raw.get("pid")
        starttime = raw.get("starttime")
        acquired = raw.get("acquired_epoch")
        return cls(
            name=name,
            state=str(raw.get("state") or STATE_FREE),
            holder=str(raw.get("holder") or ""),
            pid=int(pid) if isinstance(pid, int) else None,
            starttime=int(starttime) if isinstance(starttime, int) else None,
            acquired_epoch=float(acquired) if isinstance(acquired, int | float) else 0.0,
            waiting_for=str(raw["waiting_for"]) if raw.get("waiting_for") else None,
            wanting=str(raw.get("wanting") or ""),
            note=str(raw.get("note") or ""),
        )

    @property
    def identity(self) -> ProcIdentity | None:
        if self.pid is None or self.starttime is None:
            return None
        return ProcIdentity(pid=self.pid, starttime=self.starttime)

    def age_s(self, now: float) -> float:
        return max(0.0, now - self.acquired_epoch)


class LockRegistry:
    """Read/write the advisory records beside the authoritative flocks."""

    def __init__(self, operator: str, *, proc_root: Path = Path("/proc")) -> None:
        self.operator = operator
        self.proc_root = proc_root

    @property
    def directory(self) -> Path:
        return locks_dir(self.operator)

    def path_for(self, name: str) -> Path:
        return record_path(self.directory, name)

    def flock_for(self, name: str) -> Path:
        return flock_path(self.directory, name)

    def write(self, record: LockRecord) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(record.name)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(record.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    def read(self, name: str) -> LockRecord | None:
        try:
            raw = json.loads(self.path_for(name).read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            return None
        return LockRecord.from_dict(raw) if isinstance(raw, dict) else None

    def all_records(self) -> dict[str, LockRecord]:
        """Every recorded lock, by name. Unreadable files are skipped."""
        if not self.directory.is_dir():
            return {}
        out: dict[str, LockRecord] = {}
        for path in sorted(self.directory.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except OSError, json.JSONDecodeError:
                continue
            if isinstance(raw, dict):
                record = LockRecord.from_dict(raw)
                if record is not None:
                    out[record.name] = record
        return out

    # ------------------------------------------------------------------ intent

    def mark_waiting(
        self,
        name: str,
        *,
        holder: str,
        pid: int | None,
        starttime: int | None,
        waiting_for: str | None,
        wanting: str = "",
        now: float | None = None,
    ) -> LockRecord:
        """Record that *holder* wants *name* but cannot have it yet.

        Written **before** attempting the flock, which is the whole point: if
        the attempt blocks, the graph edge exists for the deadlock detector to
        find. Writing it after would only ever record successes.
        """
        record = LockRecord(
            name=name,
            state=STATE_WAITING,
            holder=holder,
            pid=pid,
            starttime=starttime,
            acquired_epoch=now if now is not None else time.time(),
            waiting_for=waiting_for,
            wanting=wanting,
        )
        self.write(record)
        return record

    def mark_held(
        self,
        name: str,
        *,
        holder: str,
        pid: int | None,
        starttime: int | None,
        wanting: str = "",
        now: float | None = None,
    ) -> LockRecord:
        """Record *holder* as owning *name*.

        ``wanting`` is carried over from the acquisition so a held record still
        says what its holder is in there doing — the first thing an operator
        reads when a merge lock has been held for two hours.
        """
        record = LockRecord(
            name=name,
            state=STATE_HELD,
            holder=holder,
            pid=pid,
            starttime=starttime,
            acquired_epoch=now if now is not None else time.time(),
            wanting=wanting,
        )
        self.write(record)
        return record

    def release(self, name: str, *, note: str = "") -> LockRecord | None:
        """Mark a lock free. Returns the record that was there, if any."""
        previous = self.read(name)
        if previous is None:
            return None
        self.write(
            LockRecord(
                name=name,
                state=STATE_FREE,
                acquired_epoch=previous.acquired_epoch,
                note=note,
            )
        )
        return previous

    def forget(self, name: str) -> bool:
        """Delete a record outright. Used when a stale record is unresolvable."""
        try:
            self.path_for(name).unlink()
        except OSError:
            return False
        return True

    # ------------------------------------------------------------------ holding

    @contextmanager
    def hold(
        self,
        name: str,
        *,
        holder: str = "serve",
        pid: int | None = None,
        starttime: int | None = None,
        wanting: str = "",
        now: float | None = None,
    ) -> Iterator[bool]:
        """Take a lock, recording intent on both the wait and the acquisition.

        Yields False when the lock is already held, having first written a
        ``waiting`` record naming it. The caller decides what to do; the
        registry never blocks and never retries, because a supervisor that
        blocks is a supervisor that cannot answer ``serve status``.
        """
        if pid is None:
            pid = os.getpid()
        if starttime is None:
            from agent_fleet.serve.procs import starttime_fingerprint

            starttime = starttime_fingerprint(pid, proc_root=self.proc_root)

        self.directory.mkdir(parents=True, exist_ok=True)
        with exclusive_lock(self.flock_for(name)) as acquired:
            if not acquired:
                self.mark_waiting(
                    name,
                    holder=holder,
                    pid=pid,
                    starttime=starttime,
                    waiting_for=None,
                    wanting=wanting,
                    now=now,
                )
                yield False
                return
            self.mark_held(
                name, holder=holder, pid=pid, starttime=starttime, wanting=wanting, now=now
            )
            try:
                yield True
            finally:
                self.release(name)

    # -------------------------------------------------------------- inspection

    def stale_locks(self, *, now: float, grace_minutes: float) -> list[LockRecord]:
        """Held locks whose holder is gone, older than the grace period.

        A grace period is not decoration: a lock acquired microseconds before
        its holder died would otherwise look stale, and releasing it would let
        a second component into a critical section the first is still finishing
        its exit from.
        """
        grace_s = max(0.0, grace_minutes) * 60.0
        out: list[LockRecord] = []
        for record in self.all_records().values():
            if record.state != STATE_HELD:
                continue
            holder_gone = record.pid is None or not pid_alive(record.pid, proc_root=self.proc_root)
            if holder_gone and record.age_s(now) >= grace_s:
                out.append(record)
        return out

    def deadlocks(self, *, now: float, threshold_minutes: float) -> list[list[LockRecord]]:
        """Cycles among waiters, each edge older than the threshold.

        A cycle is ``A waits for L, L held by B, B waits for M, M held by A``.
        Returned oldest-edge-first so the remediation releases the *older* claim,
        which is the one whose owner is least likely to be making progress.
        """
        records = self.all_records()
        threshold_s = max(0.0, threshold_minutes) * 60.0
        cycles: list[list[LockRecord]] = []
        seen: set[tuple[str, ...]] = set()

        for start in records.values():
            if start.state != STATE_WAITING or not start.waiting_for:
                continue
            path: list[LockRecord] = [start]
            visited = {start.name}
            current = start
            while current.waiting_for:
                target = records.get(current.waiting_for)
                if target is None:
                    break
                if target.state == STATE_HELD and target.holder == current.holder:
                    break
                if target.name in visited:
                    break
                path.append(target)
                visited.add(target.name)
                nxt = records.get(target.holder) if target.holder else None
                if nxt is None or nxt.state != STATE_WAITING or not nxt.waiting_for:
                    break
                current = nxt
            if len(path) < 2:
                continue
            oldest_edge_age = min(
                (
                    records[r.waiting_for].age_s(now)
                    for r in path
                    if r.waiting_for and r.waiting_for in records
                ),
                default=0.0,
            )
            if oldest_edge_age < threshold_s:
                continue
            key = tuple(sorted(r.name for r in path))
            if key in seen:
                continue
            seen.add(key)
            cycles.append(path)
        return cycles


__all__ = [
    "STATE_FREE",
    "STATE_HELD",
    "STATE_WAITING",
    "LockRecord",
    "LockRegistry",
    "flock_path",
    "record_path",
]
