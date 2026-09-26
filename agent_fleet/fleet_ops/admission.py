"""Admission pools for lane subprocesses, enforced through a generated ``uv`` shim.

Every lane runs a coding agent, and those agents run ``uv run pytest`` and
``uv run pyright``/``pre-commit`` freely. Left alone, twenty lanes each start
their own test suite the moment they are told to, and the box collapses.

The fix is a shared budget in :mod:`agent_fleet.slots` — flock-based, so it is
cross-process and survives a ``SIGKILL`` — reached through a **generated shim
directory placed first on the engine's ``PATH``**. A matching ``uv run pytest``
then waits for one of N slots; everything else passes straight through.

Three details are load-bearing, and each is a real bug if missed:

* **The shim must not resolve to itself.** The real ``uv`` is located with the
  shim directory stripped from ``PATH`` first, or the second invocation of the
  shim re-enters the shim and recurses.

* **The slot fd must survive ``exec``.** :func:`os.execv` replaces the process
  image but keeps open fds, and the ``flock`` lives on the fd. Python opens
  files with ``O_CLOEXEC`` by default, so without
  :func:`os.set_inheritable` the lock is released the instant the real ``uv``
  starts — and the pool admits everybody. The bash shim got this for free by
  holding fd 207 open across ``exec nice``; a Python implementation has to ask.

* **The lock is held for the child's whole life, not the shim's.** The shim
  ``exec``s rather than forks precisely so there is no window between "slot
  acquired" and "real uv running" in which the slot is not held.

Pools are named and sized from config and live in a *shared* directory, so the
budget is machine-wide across every operator — the same slots two operators'
lanes contend for, which is the point: the constraint is the hardware.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from agent_fleet.fleet_paths import agent_fleet_home
from agent_fleet.slots import SlotPool

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

#: Admission pools. Distinct names from the gate's own ``agent``/``test`` pools:
#: lane admission and gate admission are separate budgets, and a lane must not
#: be able to consume the gate's reviewer slots.
TEST_POOL = "lane-tests"
TYPECHECK_POOL = "lane-typecheck"

DEFAULT_TEST_SLOTS = 12
DEFAULT_TYPECHECK_SLOTS = 4

#: Admitted work runs niced, so a lane's test suite yields to a lane that is
#: about to commit and push.
DEFAULT_NICE = 5

#: How long the shim waits for a slot before giving up, in seconds. Long enough
#: to ride out a wave, short enough that a wedged pool is visible rather than an
#: agent that appears to hang forever.
DEFAULT_WAIT_S = 3600.0

#: The tools that take a slot, keyed to the pool they draw from.
_POOL_FOR_TOOL: dict[str, str] = {
    "pytest": TEST_POOL,
    "pyright": TYPECHECK_POOL,
    "pre-commit": TYPECHECK_POOL,
}

SHIM_NAME = "uv"


def default_admission_dir() -> Path:
    """``~/.agent-fleet/admission`` (honours ``AGENT_FLEET_HOME``)."""
    return agent_fleet_home() / "admission"


@dataclass(frozen=True)
class AdmissionConfig:
    """The admission budget, resolved once per lane.

    ``shared_dir`` is what makes the pools *shared*: two operators pointing at
    the same directory contend for the same slots, and two pointing at different
    directories do not. The default is machine-global, which is the honest
    default — the slots describe the box, not the repo.
    """

    shared_dir: Path | None = None
    tests: int = DEFAULT_TEST_SLOTS
    typecheck: int = DEFAULT_TYPECHECK_SLOTS
    nice: int = DEFAULT_NICE
    wait_s: float = DEFAULT_WAIT_S

    @property
    def root(self) -> Path:
        return Path(self.shared_dir).expanduser() if self.shared_dir else default_admission_dir()

    def slots_dir(self) -> Path:
        return self.root / "slots"

    def locks_dir(self) -> Path:
        return self.root / "locks"

    def shims_dir(self) -> Path:
        return self.root / "shims"

    def pool_size(self, pool: str) -> int:
        return self.typecheck if pool == TYPECHECK_POOL else self.tests

    def with_shared_dir(self, path: Path | str | None) -> AdmissionConfig:
        return replace(self, shared_dir=Path(path).expanduser() if path else None)


def classify(argv: list[str] | tuple[str, ...]) -> str | None:
    """Which admission pool *argv* draws from, or None to pass straight through.

    Only the exact invocations that are actually expensive are admitted:

    * ``run ... pytest``            -> the test pool
    * ``run ... pyright|pre-commit`` -> the typecheck pool
    * anything else                  -> no pool

    ``uv sync``, ``uv --version``, and ``uv run python -c 1`` are all
    unadmitted on purpose. Queuing them behind a full test pool would stall every
    lane's setup for no benefit.
    """
    args = [str(a) for a in argv]
    if "run" not in args:
        return None
    for token in args:
        base = Path(token).name
        pool = _POOL_FOR_TOOL.get(base)
        if pool is not None:
            return pool
    return None


def real_uv(env: Mapping[str, str], *, skip_dirs: Path | None = None) -> str | None:
    """Locate the real ``uv``, ignoring the shim directory.

    Resolving against an unmodified ``PATH`` would let a shim find itself. The
    shim dir is removed first, and *skip_dirs* removes any extra one the caller
    wants excluded.
    """
    raw = env.get("PATH", "")
    excluded = {str(Path(skip_dirs).expanduser())} if skip_dirs is not None else set()
    parts = [p for p in raw.split(os.pathsep) if p and str(Path(p).expanduser()) not in excluded]
    return shutil.which(SHIM_NAME, path=os.pathsep.join(parts))


def _shim_source(real: str, config: AdmissionConfig) -> str:
    """The generated shim program.

    Deliberately **self-contained**: it uses only the standard library, so it
    cannot fail to import inside a lane's worktree venv and cannot drift from
    this module. The classification table is rendered from
    :data:`_POOL_FOR_TOOL`, and ``tests/test_fleet_ops_admission.py`` asserts the
    rendered copy still equals the live one — a rename cannot silently stop
    admitting.

    The slot layout matches :class:`agent_fleet.slots.SlotPool` exactly
    (``<root>/<pool>/slot.<i>``), so a lane's test run contends with the gate's
    own pool if they are ever pointed at the same root.
    """
    table = ", ".join(f"{tool!r}: {pool!r}" for tool, pool in sorted(_POOL_FOR_TOOL.items()))
    return f'''#!{sys.executable}
"""Generated by agent-fleet. Do not edit; regenerated per lane.

A `uv run pytest` waits for one of {config.tests} shared test slots, and a
`uv run pyright`/`pre-commit` for one of {config.typecheck} typecheck slots.
The slots are flock files under {config.slots_dir()}, shared by every operator.
"""

import fcntl
import os
import sys
import time

#: Pool name -> slot count. Keyed by the *pool* name (not a short label) so the
#: on-disk directory and the size table can never drift apart.
SIZES = {{
    {TEST_POOL!r}: {config.tests},
    {TYPECHECK_POOL!r}: {config.typecheck},
}}
POOLS = {{{table}}}
ROOT = {str(config.slots_dir())!r}
REAL = {real!r}
WAIT_S = {config.wait_s!r}
NICE = {config.nice!r}

#: Must match agent_fleet.slots._POLL_INTERVAL_S so both implementations poll
#: the same slots at the same cadence.
POLL_S = 0.25


def classify(argv):
    """Return the admission pool for *argv*, or None to pass straight through."""
    if "run" not in argv:
        return None
    for token in argv:
        pool = POOLS.get(os.path.basename(token))
        if pool is not None:
            return pool
    return None


def acquire(pool, size):
    """flock one of *size* slot files in *pool*, waiting for a free one.

    A slot is free when LOCK_EX|LOCK_NB succeeds. The fd is deliberately not
    closed on success: the caller execs the real uv, and the lock has to be held
    for that process's whole life.
    """
    directory = os.path.join(ROOT, pool)
    os.makedirs(directory, exist_ok=True)
    deadline = time.monotonic() + WAIT_S
    while True:
        for index in range(size):
            fd = os.open(os.path.join(directory, f"slot.{{index}}"), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                continue
            return fd
        if time.monotonic() >= deadline:
            sys.exit(f"agent-fleet admission: no free {{pool}} slot after {{WAIT_S}}s")
        time.sleep(POLL_S)


def main():
    argv = sys.argv[1:]
    pool = classify(argv)
    if pool is None:
        # Anything that is not an admitted tool run passes straight through and
        # takes no slot, so `uv sync` and `uv --version` never queue.
        os.execv(REAL, [REAL, *argv])

    fd = acquire(pool, SIZES[pool])

    # The flock lives on this fd. os.execv keeps fds open, but Python opens them
    # O_CLOEXEC by default -- without this the kernel drops the lock the moment
    # the real uv starts and the pool admits everybody.
    os.set_inheritable(fd, True)

    if NICE:
        os.nice(NICE)

    # exec, not fork: no window in which the slot is held but the real uv is not
    # yet running.
    os.execv(REAL, [REAL, *argv])


if __name__ == "__main__":
    main()
'''


def write_shim(directory: Path | str, *, real: str, config: AdmissionConfig) -> Path:
    """Write the shim into *directory* and return its path. Idempotent.

    A *directory* is passed rather than the shared root because each lane gets
    its own: two lanes in the same worktree would otherwise race to rewrite one
    file, and a lane must never pick up a shim built for another lane's
    configuration.
    """
    target = Path(directory).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    path = target / SHIM_NAME
    path.write_text(_shim_source(real, config), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def shim_dir(config: AdmissionConfig, *, operator: str, lane: str) -> Path:
    """The per-lane shim directory under the shared root.

    Namespaced by operator then lane, so two operators never share a shim
    directory and a lane always resolves the same one across restarts.
    """
    return config.shims_dir() / operator / lane


def shim_env(
    env: Mapping[str, str],
    *,
    config: AdmissionConfig | None = None,
    operator: str = "",
    lane: str = "",
) -> dict[str, str]:
    """The environment overlay that puts the admission shim on the engine's PATH.

    Returns *env* unchanged when no shim could be built (no real ``uv`` on the
    box, or an unwritable directory) — admission is a throttle, and a throttle
    that breaks the lane it is meant to protect is worse than no throttle.
    """
    conf = config or AdmissionConfig()
    if not operator or not lane:
        return dict(env)

    directory = shim_dir(conf, operator=operator, lane=lane)
    real = real_uv(env, skip_dirs=directory)
    if not real:
        logger.warning("admission: no real uv on PATH; lane %s/%s runs unadmitted", operator, lane)
        return dict(env)

    try:
        write_shim(directory, real=real, config=conf)
    except OSError as exc:
        logger.warning("admission: could not write shim into %s: %s", directory, exc)
        return dict(env)

    overlay = dict(env)
    overlay["PATH"] = f"{directory}{os.pathsep}{env.get('PATH', '')}"
    overlay["AGENT_FLEET_ADMISSION_DIR"] = str(directory)
    overlay["AGENT_FLEET_ADMISSION_TESTS"] = str(conf.tests)
    overlay["AGENT_FLEET_ADMISSION_TYPECHECK"] = str(conf.typecheck)
    return overlay


def pool_for_label(label: str, config: AdmissionConfig | None = None) -> SlotPool:
    """The :class:`SlotPool` for a ``tests``/``typecheck`` label.

    Named for the shim's use, where the config has already been baked into the
    generated source; the dispatcher uses :meth:`AdmissionConfig.pool_size`.
    """
    conf = config or AdmissionConfig()
    return SlotPool(label, root=conf.slots_dir(), size=conf.pool_size(label))


__all__ = [
    "DEFAULT_NICE",
    "DEFAULT_TEST_SLOTS",
    "DEFAULT_TYPECHECK_SLOTS",
    "SHIM_NAME",
    "TEST_POOL",
    "TYPECHECK_POOL",
    "AdmissionConfig",
    "classify",
    "default_admission_dir",
    "pool_for_label",
    "real_uv",
    "shim_dir",
    "shim_env",
    "write_shim",
]
