"""Shared cursor-sdk-bridge daemon for concurrent agent-fleet runs.

The cursor-sdk Python client, by default, spawns a private bridge subprocess
the first time it needs one. That makes it impossible to run several
`agent-fleet run` processes side-by-side: each tries to spin up its own
bridge and they race on shared resources.

cursor-sdk already supports connecting to an externally-managed bridge via
the env vars `CURSOR_SDK_BRIDGE_URL` and `CURSOR_SDK_BRIDGE_TOKEN`. This
module launches one long-lived bridge (optionally under a supervisor that
respawns it on crash), persists its discovery info to
`~/.agent-fleet/bridge.json`, and exposes helpers so the cursor backend
can transparently attach to it.

Run as a module to enter supervisor mode:
    python -m agent_fleet.bridge_daemon --supervisor --workspace ~/.agent-fleet
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from agent_fleet.fleet_paths import agent_fleet_home, ensure_agent_fleet_home

logger = logging.getLogger(__name__)

_READY_PREFIX = "cursor-sdk-bridge ready "
_DEFAULT_TIMEOUT_S = 30.0
_SUPERVISOR_BACKOFF_MAX_S = 30.0
_HEALTH_CHECK_TIMEOUT_S = 1.5


def _bridge_url_responsive(url: str, timeout_s: float = _HEALTH_CHECK_TIMEOUT_S) -> bool:
    """Probe the bridge URL with a fast TCP connect.

    Guards against the common failure mode where bridge.json points at a
    URL whose listener is gone (supervisor was killed, node crashed without
    the supervisor cleaning state, port was reclaimed) — attaching env vars
    to such a URL turns every cursor-sdk call into a ConnectError.
    """
    import socket
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = parsed.hostname
    port = parsed.port
    if not host or port is None:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def bridge_state_path() -> Path:
    return agent_fleet_home() / "bridge.json"


def bridge_log_path() -> Path:
    return agent_fleet_home() / "bridge.log"


def supervisor_pid_path() -> Path:
    return agent_fleet_home() / "bridge-supervisor.pid"


def supervisor_log_path() -> Path:
    return agent_fleet_home() / "bridge-supervisor.log"


def _resolve_bridge_binary() -> str:
    from cursor_sdk._vendor import resolve_bridge_path

    return str(resolve_bridge_path())


def load_bridge_state() -> dict[str, Any] | None:
    path = bridge_state_path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return None


def _load_supervisor_pid() -> int | None:
    path = supervisor_pid_path()
    if not path.is_file():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except OSError, ValueError:
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


def _read_discovery_from_log(log_path: Path, deadline: float) -> dict[str, Any]:
    """Tail the bridge stderr log until the ready discovery line appears."""
    while time.monotonic() < deadline:
        if log_path.is_file():
            with log_path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith(_READY_PREFIX):
                        payload = line[len(_READY_PREFIX) :].strip()
                        return json.loads(payload)
        time.sleep(0.1)
    raise TimeoutError(f"Bridge did not emit discovery within deadline ({log_path})")


def _discovery_to_state(discovery: dict[str, Any], pid: int) -> dict[str, Any]:
    url = discovery.get("url")
    if not url:
        host = discovery.get("host", "")
        port = discovery.get("port")
        if host and port is not None:
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            url = f"http://{host}:{port}"
    auth_token = (discovery.get("authToken") or "").strip()
    if not auth_token:
        token_file = discovery.get("authTokenFile")
        if token_file:
            try:
                auth_token = Path(str(token_file)).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ValueError(f"Could not read authTokenFile {token_file}: {exc}") from exc
    if not url or not auth_token:
        raise ValueError("Bridge discovery missing url or authToken")
    return {
        "url": url,
        "auth_token": auth_token,
        "pid": pid,
        "workspace_ref": discovery.get("workspaceRef", ""),
        "server_version": discovery.get("serverVersion", ""),
        "schema_version": discovery.get("schemaVersion", 1),
        "started_at": time.time(),
    }


def _spawn_bridge_subprocess(workspace: str, log_path: Path) -> subprocess.Popen:
    # cursor-sdk-bridge has a documented opt-in env var that installs
    # process-level uncaughtException/unhandledRejection survivors. Without
    # it, an EPIPE on a peer-disconnect socket bubbles up as an unhandled
    # stream 'error' event and kills the node process, which is fatal when
    # multiple concurrent agent-fleet runs share one bridge.
    env = {**os.environ, "CURSOR_SDK_BRIDGE_SURVIVE_UNCAUGHT": "1"}
    log_fh = log_path.open("ab", buffering=0)
    try:
        return subprocess.Popen(
            [_resolve_bridge_binary(), "--workspace", workspace],
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
    finally:
        log_fh.close()


def _publish_bridge_state(discovery: dict[str, Any], pid: int) -> dict[str, Any]:
    state = _discovery_to_state(discovery, pid)
    bridge_state_path().write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def _rotate_bridge_log(log_path: Path) -> None:
    """Preserve a crashed bridge's stderr for post-mortem diagnosis."""
    if not log_path.is_file() or log_path.stat().st_size == 0:
        log_path.unlink(missing_ok=True)
        return
    ts = time.strftime("%Y%m%dT%H%M%S")
    rotated = log_path.with_name(f"{log_path.name}.crashed-{ts}")
    with contextlib.suppress(OSError):
        log_path.rename(rotated)
    log_path.unlink(missing_ok=True)


def _launch_one_bridge(workspace: str, timeout_s: float) -> tuple[subprocess.Popen, dict[str, Any]]:
    log_path = bridge_log_path()
    _rotate_bridge_log(log_path)
    proc = _spawn_bridge_subprocess(workspace, log_path)
    deadline = time.monotonic() + timeout_s
    try:
        discovery = _read_discovery_from_log(log_path, deadline)
    except Exception:
        with contextlib.suppress(Exception):
            proc.terminate()
        raise
    state = _publish_bridge_state(discovery, proc.pid)
    return proc, state


def _supervisor_loop(workspace: str, timeout_s: float) -> int:
    """Long-running loop: respawn the bridge whenever it exits."""
    pid_path = supervisor_pid_path()
    pid_path.write_text(str(os.getpid()), encoding="utf-8")

    child: dict[str, subprocess.Popen | None] = {"proc": None}
    stopping = {"flag": False}

    def _on_signal(_signum: int, _frame: object) -> None:
        stopping["flag"] = True
        proc = child["proc"]
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(Exception):
                proc.terminate()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    backoff = 1.0
    try:
        while not stopping["flag"]:
            try:
                proc, _ = _launch_one_bridge(workspace, timeout_s)
                child["proc"] = proc
                backoff = 1.0
                exit_code = proc.wait()
                logger.warning("bridge exited (status=%s); will respawn", exit_code)
            except Exception as exc:
                logger.exception("bridge launch failed: %s", exc)
            finally:
                child["proc"] = None
                bridge_state_path().unlink(missing_ok=True)
            if stopping["flag"]:
                break
            time.sleep(backoff)
            backoff = min(backoff * 2.0, _SUPERVISOR_BACKOFF_MAX_S)
    finally:
        pid_path.unlink(missing_ok=True)
        bridge_state_path().unlink(missing_ok=True)
    return 0


def start_bridge(
    *,
    workspace: Path | str | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    supervise: bool = True,
) -> dict[str, Any]:
    """Start a detached bridge daemon and persist its discovery info.

    If a healthy daemon is already recorded in `~/.agent-fleet/bridge.json`,
    return its state unchanged (idempotent). When `supervise=True` (default)
    the bridge runs under a parent supervisor that respawns it on crash.
    """
    ensure_agent_fleet_home()
    existing = load_bridge_state()
    if existing and _pid_alive(int(existing.get("pid", 0))):
        return existing

    workspace_arg = str(workspace) if workspace else str(agent_fleet_home())

    if not supervise:
        _, state = _launch_one_bridge(workspace_arg, timeout_s)
        return state

    # Tear down any stale supervisor before launching a new one.
    _terminate_supervisor()

    supervisor_log = supervisor_log_path().open("ab", buffering=0)
    try:
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_fleet.bridge_daemon",
                "--supervisor",
                "--workspace",
                workspace_arg,
                "--timeout",
                str(timeout_s),
            ],
            stdout=supervisor_log,
            stderr=supervisor_log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        supervisor_log.close()

    deadline = time.monotonic() + timeout_s + 5.0
    while time.monotonic() < deadline:
        state = load_bridge_state()
        if state and _pid_alive(int(state.get("pid", 0))):
            return state
        time.sleep(0.2)
    raise TimeoutError("Supervisor did not publish bridge state in time")


def _terminate_supervisor() -> bool:
    pid = _load_supervisor_pid()
    if pid is None or not _pid_alive(pid):
        supervisor_pid_path().unlink(missing_ok=True)
        return False
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.1)
    if _pid_alive(pid):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    supervisor_pid_path().unlink(missing_ok=True)
    return True


def stop_bridge() -> dict[str, Any]:
    """Terminate the supervisor (if any) and the bridge, remove state."""
    result: dict[str, Any] = {"stopped": False}
    supervisor_pid = _load_supervisor_pid()
    if supervisor_pid is not None:
        result["supervisor_pid"] = supervisor_pid
        if _terminate_supervisor():
            result["supervisor_stopped"] = True

    state = load_bridge_state()
    if state is None:
        result.setdefault("reason", "no bridge.json found")
        result["stopped"] = bool(result.get("supervisor_stopped"))
        return result
    pid = int(state.get("pid", 0) or 0)
    result["pid"] = pid
    if pid > 0 and _pid_alive(pid):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and _pid_alive(pid):
            time.sleep(0.1)
        if _pid_alive(pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
    bridge_state_path().unlink(missing_ok=True)
    result["stopped"] = True
    return result


def status_bridge() -> dict[str, Any]:
    state = load_bridge_state()
    supervisor_pid = _load_supervisor_pid()
    supervised = supervisor_pid is not None and _pid_alive(supervisor_pid)
    if state is None:
        return {
            "running": False,
            "supervised": supervised,
            "supervisor_pid": supervisor_pid,
            "reason": "no bridge.json",
        }
    pid = int(state.get("pid", 0) or 0)
    alive = _pid_alive(pid)
    return {
        "running": alive,
        "supervised": supervised,
        "supervisor_pid": supervisor_pid,
        "pid": pid,
        "url": state.get("url"),
        "workspace_ref": state.get("workspace_ref"),
        "server_version": state.get("server_version"),
        "started_at": state.get("started_at"),
    }


def apply_bridge_env(*, force: bool = False, wait_s: float = 0.0) -> bool:
    """If a healthy bridge daemon is recorded, export its endpoint to env.

    Returns True when env was populated (caller can rely on shared bridge),
    False otherwise. When ``force`` is False, existing env vars take
    precedence; pass ``force=True`` on reconnect after a supervisor respawn
    so the new (different) URL/token replace the stale ones. ``wait_s``
    polls for bridge.json to (re)appear, which happens after a respawn.
    """
    if (
        not force
        and os.environ.get("CURSOR_SDK_BRIDGE_URL")
        and (
            os.environ.get("CURSOR_SDK_BRIDGE_TOKEN")
            or os.environ.get("CURSOR_SDK_BRIDGE_AUTH_TOKEN")
        )
    ):
        return True
    deadline = time.monotonic() + max(0.0, wait_s)
    state = load_bridge_state()
    while state is None and time.monotonic() < deadline:
        time.sleep(0.2)
        state = load_bridge_state()
    if state is None:
        return False
    pid = int(state.get("pid", 0) or 0)
    if not _pid_alive(pid):
        return False
    url = str(state.get("url", "")).strip()
    token = str(state.get("auth_token", "")).strip()
    if not url or not token:
        return False
    if not _bridge_url_responsive(url):
        # Stale bridge.json — pid is alive but the HTTP listener is gone.
        # Refuse to publish dead endpoint env; caller falls back to per-process bridge.
        return False
    os.environ["CURSOR_SDK_BRIDGE_URL"] = url
    os.environ["CURSOR_SDK_BRIDGE_TOKEN"] = token
    os.environ["CURSOR_SDK_BRIDGE_AUTH_TOKEN"] = token
    return True


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent_fleet.bridge_daemon")
    parser.add_argument("--supervisor", action="store_true", help="Run the supervisor loop")
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--timeout", type=float, default=_DEFAULT_TIMEOUT_S)
    args = parser.parse_args(argv)
    if not args.supervisor:
        parser.print_help()
        return 2
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s bridge-supervisor %(message)s",
    )
    workspace = args.workspace or str(agent_fleet_home())
    ensure_agent_fleet_home()
    return _supervisor_loop(workspace, args.timeout)


if __name__ == "__main__":
    sys.exit(_main())
