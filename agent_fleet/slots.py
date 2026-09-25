"""Machine-wide cross-process concurrency slots.

Fleet's in-process :class:`~agent_fleet.admission.AdmissionController` bounds
concurrency inside one process. Several independent fleet processes (the gate
pipeline, a watch daemon, an ad-hoc ``fleet run``) would each get their own
budget, so N processes could fan out to N x ``max_parallel`` agents and
exhaust the machine. This module supplies a single machine-wide budget they all
share.

A slot is a file descriptor held open with an advisory ``flock``. The kernel
drops the lock when the holding process exits — however it exits, including
``SIGKILL`` — so a crashed gate run cannot leak slots. No cleanup bookkeeping,
no stale-pid reaping, no lock file to corrupt.

Layout under ``~/.agent-fleet/slots``::

    <pool>/slot.0   ... slot.<size-1>   one lock file per slot
    <pool>/pool.json                     the pool's declared size

A pool may shrink between runs (an operator lowering the budget) but never
grows implicitly past its recorded size: a process that would exceed it blocks
rather than creating slots another process does not know about. Raise the size
by editing ``pool.json`` or passing a larger ``size=`` once no run is active.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_fleet.fleet_paths import agent_fleet_home

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)

DEFAULT_POOL_NAME = "agent"
DEFAULT_POOL_SIZE = 24
DEFAULT_TEST_POOL_NAME = "test"
DEFAULT_TEST_POOL_SIZE = 4

_POLL_INTERVAL_S = 0.25
_LOCK_NB = fcntl.LOCK_EX | fcntl.LOCK_NB


class SlotPoolFull(Exception):
    """Raised by :meth:`SlotPool.acquire` when no slot frees within the timeout."""


@dataclass(frozen=True)
class PoolConfig:
    """Machine-wide budgets, resolved once per run."""

    root: Path
    agent_slots: int = DEFAULT_POOL_SIZE
    test_slots: int = DEFAULT_TEST_POOL_SIZE
    poll_interval_s: float = _POLL_INTERVAL_S


def default_slots_root() -> Path:
    """``~/.agent-fleet/slots`` (honours ``AGENT_FLEET_HOME``)."""
    return agent_fleet_home() / "slots"


def _pool_dir(root: Path | str, name: str) -> Path:
    return Path(root) / name


def _record_path(root: Path | str, name: str) -> Path:
    return _pool_dir(root, name) / "pool.json"


def declared_size(root: Path | str, name: str) -> int | None:
    """Return the size recorded in ``pool.json``, or ``None`` if undeclared."""
    path = _record_path(root, name)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return None
    size = data.get("size") if isinstance(data, dict) else None
    return int(size) if isinstance(size, int) and size > 0 else None


def record_size(root: Path | str, name: str, size: int) -> None:
    """Persist the pool size so later processes agree on the budget."""
    if size <= 0:
        raise ValueError(f"pool size must be positive, got {size}")
    path = _record_path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"size": int(size), "updated_at": time.time()})
    last: OSError | None = None
    for attempt in range(3):
        try:
            # One tmp file PER WRITER (a shared tmp name let one process's
            # os.replace move the file out from under another) and an flock so
            # concurrent read-modify-write cycles serialize.
            with (path.parent / "pool.lock").open("a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="pool.", suffix=".tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        fh.write(payload)
                    Path(tmp).replace(path)
                finally:
                    with contextlib.suppress(FileNotFoundError):
                        Path(tmp).unlink()
            return
        except OSError as exc:  # bookkeeping IO must not fail a whole gate
            last = exc
            time.sleep(0.05 * (attempt + 1))
    raise last if last else OSError("record_size failed")


class _Slot:
    """One held lock file descriptor. Releasing is idempotent."""

    __slots__ = ("_fd", "_path")

    def __init__(self, path: Path, fd: int) -> None:
        self._path = path
        self._fd = fd

    @property
    def path(self) -> Path:
        return self._path

    def release(self) -> None:
        if self._fd == -1:
            return
        fd, self._fd = self._fd, -1
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)


class SlotPool:
    """A named, cross-process, flock-based concurrency budget.

    ``size`` slots live at ``<root>/<name>/slot.<i>``. A slot is free when
    ``flock(LOCK_EX | LOCK_NB)`` succeeds. Acquisition is first-come-first-served
    with a bounded wait, so a wide fan-out queues instead of failing the run.
    """

    def __init__(
        self,
        name: str = DEFAULT_POOL_NAME,
        *,
        root: Path | str | None = None,
        size: int | None = None,
        poll_interval_s: float = _POLL_INTERVAL_S,
    ) -> None:
        self.name = name
        self.root = Path(root) if root is not None else default_slots_root()
        self._size = size
        self._poll_interval_s = poll_interval_s

    @property
    def size(self) -> int:
        """Effective slot count: the configured size, else the recorded one."""
        if self._size is not None:
            return self._size
        return declared_size(self.root, self.name) or DEFAULT_POOL_SIZE

    @property
    def dir(self) -> Path:
        return _pool_dir(self.root, self.name)

    def _slot_path(self, index: int) -> Path:
        return self.dir / f"slot.{index}"

    def acquire(self, *, timeout_s: float | None = None) -> _Slot:
        """Take one slot, waiting up to *timeout_s* (``None`` = wait forever)."""
        size = self.size
        record_size(self.root, self.name, size)
        self.dir.mkdir(parents=True, exist_ok=True)
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        while True:
            for index in range(size):
                path = self._slot_path(index)
                try:
                    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
                except OSError as exc:  # pragma: no cover - unusual fs failure
                    logger.debug("slot open failed for %s: %s", path, exc)
                    continue
                try:
                    fcntl.flock(fd, _LOCK_NB)
                except OSError:
                    os.close(fd)
                    continue
                logger.debug("acquired %s slot %d/%d", self.name, index + 1, size)
                return _Slot(path, fd)
            if deadline is not None and time.monotonic() >= deadline:
                raise SlotPoolFull(
                    f"no free slot in pool {self.name!r} ({size} slots) after {timeout_s}s"
                )
            time.sleep(self._poll_interval_s)

    def release(self, slot: _Slot) -> None:
        slot.release()

    @contextlib.contextmanager
    def slot(self, *, timeout_s: float | None = None) -> Generator[_Slot]:
        """Context-manager form: hold one slot for the duration of the block."""
        held = self.acquire(timeout_s=timeout_s)
        try:
            yield held
        finally:
            self.release(held)

    def in_use(self) -> int:
        """Count currently-held slots by probing each lock non-blockingly."""
        held = 0
        for index in range(self.size):
            path = self._slot_path(index)
            if not path.exists():
                continue
            try:
                fd = os.open(path, os.O_RDWR)
            except OSError:
                continue
            try:
                fcntl.flock(fd, _LOCK_NB)
            except OSError:
                held += 1
                os.close(fd)
                continue
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        return held


def agent_slot_pool(cfg: PoolConfig | None = None) -> SlotPool:
    """The machine-wide budget shared by every backend session."""
    conf = cfg or PoolConfig(root=default_slots_root())
    return SlotPool(
        DEFAULT_POOL_NAME,
        root=conf.root,
        size=conf.agent_slots,
        poll_interval_s=conf.poll_interval_s,
    )


def test_slot_pool(cfg: PoolConfig | None = None) -> SlotPool:
    """The smaller pool bounding concurrent pytest processes."""
    conf = cfg or PoolConfig(root=default_slots_root())
    return SlotPool(
        DEFAULT_TEST_POOL_NAME,
        root=conf.root,
        size=conf.test_slots,
        poll_interval_s=conf.poll_interval_s,
    )
