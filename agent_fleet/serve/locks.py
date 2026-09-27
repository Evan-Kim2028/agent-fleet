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
    <serve>/locks/<name>.waiting.json
                                  the same shape, for a component *waiting* for
                                  that lock: its own file, because a wait is not
                                  a hold and sharing one file meant a contender
                                  overwrote the holder's record

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
from contextlib import contextmanager, suppress
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


def intent_record_name(name: str) -> str:
    """The record name carrying a waiter's intent for *name*.

    A wait is a different fact from a hold, so it gets its own record. When
    both shared ``<name>.json`` the instant a second component contended for a
    lock it overwrote the holder's ``held`` record, which destroyed two things
    at once: the stale-holder rule could no longer see the dead holder, and the
    deadlock walk had nowhere to read a ``waiting_for`` edge from.
    """
    return f"{name}.waiting"


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

    def intent_path_for(self, name: str) -> Path:
        return record_path(self.directory, intent_record_name(name))

    def flock_for(self, name: str) -> Path:
        return flock_path(self.directory, name)

    def write(self, record: LockRecord) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(record.name)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(record.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    def read(self, name: str) -> LockRecord | None:
        """The record for *name*: the holder's, or the waiter's when unheld.

        A lock nobody holds has no holder record, but it may have a component
        queued behind it, and that is the fact worth reporting about it.
        """
        record = self._read_path(self.path_for(name))
        return record if record is not None else self._read_path(self.intent_path_for(name))

    def _read_path(self, path: Path) -> LockRecord | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
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
            except (OSError, json.JSONDecodeError):
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
        waiting_for: str | None = None,
        wanting: str = "",
        now: float | None = None,
    ) -> LockRecord:
        """Record that *holder* wants *name* but cannot have it yet.

        Written **before** attempting the flock, which is the whole point: if
        the attempt blocks, the graph edge exists for the deadlock detector to
        find. Writing it after would only ever record successes.

        *waiting_for* defaults to *name* — the lock this waiter is stuck behind.
        ``hold()`` passes nothing because "I am waiting for this lock" is the
        only edge a contention can produce, and the detector needs it spelled out
        rather than inferred from the record's own name.
        """
        record = LockRecord(
            name=intent_record_name(name),
            state=STATE_WAITING,
            holder=holder,
            pid=pid,
            starttime=starttime,
            acquired_epoch=now if now is not None else time.time(),
            waiting_for=name if waiting_for is None else waiting_for,
            wanting=wanting,
        )
        self.write(record)
        return record

    def clear_waiting(self, name: str) -> None:
        """Drop the intent record for *name*, if one is lying around.

        A waiter that is no longer waiting must not stay in the graph: a stale
        edge turns a component that has moved on into a permanent deadlock.
        """
        with suppress(OSError):
            self.intent_path_for(name).unlink(missing_ok=True)

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
                    wanting=wanting,
                    now=now,
                )
                yield False
                return
            self.clear_waiting(name)
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

    def _intent_edges(self, records: dict[str, LockRecord]) -> dict[str, LockRecord]:
        """The ``waiting`` records, keyed by the component that is blocked.

        Keyed by holder because that is what a cycle is made of: a hop names a
        lock, the lock's record names a holder, and the next hop is that
        holder's own intent. Keying by record name instead could only ever look
        for a component's intent under a lock's name, which is never where one
        is written, so no cycle would ever be closed.
        """
        edges: dict[str, LockRecord] = {}
        for record in records.values():
            if record.state != STATE_WAITING or not record.waiting_for:
                continue
            edges.setdefault(record.holder, record)
        return edges

    def _follow(
        self,
        start: LockRecord,
        records: dict[str, LockRecord],
        edges: dict[str, LockRecord],
    ) -> list[LockRecord] | None:
        """The alternating locks and intents a cycle is made of, or None.

        A cycle is ``A holds L1 and waits for L2; L2 is held by B; B waits for
        L1``. Every hop is therefore a pair -- the record for the lock whose
        holder we are chasing, then that holder's own intent -- and the walk is
        only a cycle when it closes on a component already in the path.

        What resolves a hop is the record named by ``waiting_for`` and the
        holder it names, not the record's own ``state``: a lock's record says
        ``held`` once it is taken, but a lock that two components want before
        either has it is still a lock with a holder, and refusing to walk
        through it would hide the cycle that produced the contention.

        Two things it deliberately refuses to call a cycle: a single
        ``[waiting, held]`` pair, which is an ordinary queue behind a live
        holder, and a walk that dead-ends. Neither is mutual, and reporting one
        would have the watchdog release a lock a healthy component is still
        working inside.
        """
        path: list[LockRecord] = [start]
        seen_holders = {start.holder}
        current = start
        while current.waiting_for:
            target = records.get(current.waiting_for)
            if target is None or not target.holder:
                return None
            if target.holder in seen_holders:
                return [*path, target]
            nxt = edges.get(target.holder)
            if nxt is None:
                return None
            path.extend((target, nxt))
            seen_holders.add(nxt.holder)
            current = nxt
        return None

    def deadlocks(self, *, now: float, threshold_minutes: float) -> list[list[LockRecord]]:
        """Cycles among waiters, each edge older than the threshold.

        A cycle is ``A waits for L, L held by B, B waits for M, M held by A``.
        Returned oldest-edge-first so the remediation releases the *older* claim,
        which is the one whose owner is least likely to be making progress. The
        registry is read in filename order, which says nothing about age, so the
        sort is applied here; without it the freshest cycle is remediated first
        and the claim that has been stuck for hours keeps its lock.

        The walk reads the whole registry, so it sees both kinds of record: a
        waiter's intent (state ``waiting``) and the holder's claim on the lock
        that waiter is blocked behind. Both have to be there for an edge to
        resolve, which is why the intent is written to its own file.
        """
        records = self.all_records()
        threshold_s = max(0.0, threshold_minutes) * 60.0
        edges = self._intent_edges(records)
        cycles: list[tuple[float, list[LockRecord]]] = []
        seen: set[tuple[str, ...]] = set()

        for start in edges.values():
            path = self._follow(start, records, edges)
            if path is None:
                continue
            # An edge is as old as the waiter blocked on it, which is how long
            # the queue has been stuck. The lock record's own age says how long
            # its owner has had it, which is a different fact: a healthy
            # long-running hold is not a stuck wait, and reading it that way is
            # what let an ordinary queue be reported as a deadlock.
            oldest = min((r.age_s(now) for r in path if r.state == STATE_WAITING), default=0.0)
            if oldest < threshold_s:
                continue
            key = tuple(sorted(r.name for r in path))
            if key in seen:
                continue
            seen.add(key)
            cycles.append((oldest, path))

        cycles.sort(key=lambda pair: pair[0], reverse=True)
        return [path for _age, path in cycles]


__all__ = [
    "STATE_FREE",
    "STATE_HELD",
    "STATE_WAITING",
    "LockRecord",
    "LockRegistry",
    "flock_path",
    "intent_record_name",
    "record_path",
]
