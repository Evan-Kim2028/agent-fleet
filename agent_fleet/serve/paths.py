"""Where ``fleet serve`` keeps everything, and the flock that guards it.

One supervisor owns one directory::

    $AGENT_FLEET_HOME/serve/<operator>/
        serve.pid              the supervisor's own pid, for re-attach
        serve.lock             flock held for the supervisor's lifetime
        state.json             components, crash history, last tick
        capacity.json          the AIMD targets dispatch/gate/admission read
        items.jsonl            the item board's stage transitions
        children.jsonl         every pid serve spawned, with its fingerprint
        events.jsonl           serve's own event mirror
        decisions.jsonl        fence/owner escalations awaiting a human
        components/<name>.log  each component's stdout+stderr
        components/<name>.pid  each component's pid + fingerprint
        locks/                 lock files serve's components contend on

Scoping by operator matters for the same reason it does in ``fleet_ops``: two
supervisors must be able to run against the same repos without either one
clobbering the other's capacity targets or killing the other's children.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
from contextlib import contextmanager
from typing import TYPE_CHECKING

from agent_fleet.fleet_paths import agent_fleet_home

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

#: Subdirectories created on first use.
SUBDIRS = ("components", "locks")


def serve_dir(operator: str) -> Path:
    """The supervisor's state directory for *operator*."""
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in operator) or "default"
    return agent_fleet_home() / "serve" / safe


def pid_path(operator: str) -> Path:
    return serve_dir(operator) / "serve.pid"


def lock_path(operator: str) -> Path:
    return serve_dir(operator) / "serve.lock"


def state_path(operator: str) -> Path:
    return serve_dir(operator) / "state.json"


def capacity_path(operator: str) -> Path:
    return serve_dir(operator) / "capacity.json"


def items_path(operator: str) -> Path:
    return serve_dir(operator) / "items.jsonl"


def children_path(operator: str) -> Path:
    return serve_dir(operator) / "children.jsonl"


def events_path(operator: str) -> Path:
    return serve_dir(operator) / "events.jsonl"


def decisions_path(operator: str) -> Path:
    return serve_dir(operator) / "decisions.jsonl"


def component_dir(operator: str) -> Path:
    return serve_dir(operator) / "components"


def component_log_path(operator: str, name: str) -> Path:
    return component_dir(operator) / f"{name}.log"


def component_pid_path(operator: str, name: str) -> Path:
    return component_dir(operator) / f"{name}.pid"


def locks_dir(operator: str) -> Path:
    return serve_dir(operator) / "locks"


def ensure_serve_dir(operator: str) -> Path:
    """Create the serve layout for *operator* if missing."""
    root = serve_dir(operator)
    root.mkdir(parents=True, exist_ok=True)
    for sub in SUBDIRS:
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    """Write *payload* to *path* via a temp file + rename.

    A supervisor that dies mid-write must not leave a half-written capacity
    file behind: the next reader is a foreign process deciding how many lanes to
    launch, and a truncated JSON document there reads as "no capacity
    information" rather than as an error.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict[str, object] | None:
    """Parse *path* as a JSON object, or ``None`` if absent or unusable."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return None
    return raw if isinstance(raw, dict) else None


@contextmanager
def exclusive_lock(path: Path) -> Iterator[bool]:
    """Hold an exclusive flock on *path*; yield False if someone else has it.

    The yield value is the whole point: the bash drivers used
    ``flock -n ... || exit 0``, which means "give up silently". A supervisor
    that silently skips is a supervisor whose operator cannot tell whether the
    tick happened, so callers get the boolean and record an event.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            yield False
            return
        yield True
    finally:
        os.close(handle)


def read_pidfile(path: Path) -> dict[str, object] | None:
    """Read a pid file written as JSON ``{"pid": N, "starttime": T, ...}``."""
    payload = read_json(path)
    if payload is None:
        return None
    pid = payload.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return None
    return payload


__all__ = [
    "SUBDIRS",
    "capacity_path",
    "children_path",
    "component_dir",
    "component_log_path",
    "component_pid_path",
    "decisions_path",
    "ensure_serve_dir",
    "events_path",
    "exclusive_lock",
    "items_path",
    "lock_path",
    "locks_dir",
    "pid_path",
    "read_json",
    "read_pidfile",
    "serve_dir",
    "state_path",
    "write_json_atomic",
]
