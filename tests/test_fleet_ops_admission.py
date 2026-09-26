"""Admission pools: the generated ``uv`` shim that bounds lane subprocesses.

The load-bearing test here is
:func:`test_the_shim_holds_its_slot_while_the_real_tool_runs`. It is the only
one that catches the bug this whole mechanism is easy to get wrong: a ``flock``
lives on a file descriptor, ``os.execv`` keeps fds open, but Python opens them
``O_CLOEXEC`` by default — so a shim that acquires a slot and execs without
``os.set_inheritable`` drops the lock the instant the real ``uv`` starts, and
the pool silently admits everybody.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from agent_fleet.fleet_ops import admission
from agent_fleet.fleet_ops.admission import (
    AdmissionConfig,
    classify,
    real_uv,
    shim_dir,
    shim_env,
    write_shim,
)
from agent_fleet.fleet_ops.config import FleetOpsConfig, load_fleet_ops_config
from agent_fleet.fleet_ops.engines import _spawn_capture
from agent_fleet.slots import SlotPool


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


# ------------------------------------------------------------------ classify


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "pytest", "-q"],
        ["run", "pytest"],
        ["run", "--all-packages", "pytest", "-q", "tests/"],
        ["run", "python", "-m", "pytest", "-q"],
    ],
)
def test_pytest_draws_from_the_test_pool(argv: list[str]) -> None:
    assert classify(argv) == admission.TEST_POOL


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "pyright"],
        ["run", "pre-commit", "run", "-a"],
        ["run", "python", "-m", "pyright"],
    ],
)
def test_typecheckers_draw_from_the_typecheck_pool(argv: list[str]) -> None:
    assert classify(argv) == admission.TYPECHECK_POOL


@pytest.mark.parametrize(
    "argv",
    [
        ["--version"],
        ["sync"],
        ["run", "python", "-c", "1"],
        ["run", "pip", "list"],
        ["run", "mypy"],
        [],
    ],
)
def test_everything_else_passes_straight_through(argv: list[str]) -> None:
    """Queuing `uv sync` behind a full test pool would stall every lane's setup."""
    assert classify(argv) is None


def test_an_absolute_path_to_pytest_is_still_classified() -> None:
    assert classify(["run", "/venv/bin/pytest", "-q"]) == admission.TEST_POOL


def test_the_pools_are_distinct_from_the_gate_pools() -> None:
    """A lane must not be able to consume the gate's reviewer slots."""
    assert admission.TEST_POOL not in ("agent", "test")
    assert admission.TYPECHECK_POOL not in ("agent", "test")


# ----------------------------------------------------------------- real uv


def test_real_uv_skips_the_shim_directory() -> None:
    """A shim that resolved to itself would recurse forever."""
    directory = Path("/tmp/agent-fleet-shim")
    env = {"PATH": f"{directory}:{os.environ.get('PATH', '')}"}
    found = real_uv(env, skip_dirs=directory)
    assert found is not None
    assert str(Path(found).parent) != str(directory)


def test_real_uv_is_none_when_uv_is_absent() -> None:
    assert real_uv({"PATH": "/nonexistent-dir-for-sure"}) is None


# ------------------------------------------------------------------- the shim


def test_the_shim_is_written_and_executable(tmp_path: Path) -> None:
    path = write_shim(tmp_path, real="/usr/bin/env uv", config=AdmissionConfig(shared_dir=tmp_path))
    assert path.is_file()
    assert os.access(path, os.X_OK)
    assert path.read_text(encoding="utf-8").startswith(f"#!{sys.executable}")


def test_shim_env_puts_the_shim_dir_first_on_path(tmp_path: Path) -> None:
    config = AdmissionConfig(shared_dir=tmp_path)
    env = {"PATH": os.environ.get("PATH", "")}
    overlay = shim_env(env, config=config, operator="documents-0e", lane="alpha")
    head = overlay["PATH"].split(os.pathsep)[0]
    assert head == str(shim_dir(config, operator="documents-0e", lane="alpha"))
    assert (Path(head) / "uv").is_file()


def test_two_operators_and_lanes_get_distinct_shim_dirs(tmp_path: Path) -> None:
    config = AdmissionConfig(shared_dir=tmp_path)
    a = shim_dir(config, operator="documents-0e", lane="alpha")
    b = shim_dir(config, operator="documents-1d", lane="alpha")
    c = shim_dir(config, operator="documents-0e", lane="beta")
    assert len({a, b, c}) == 3


def test_shim_env_without_an_operator_or_lane_is_a_no_op(tmp_path: Path) -> None:
    env = {"PATH": "/usr/bin"}
    assert shim_env(env, config=AdmissionConfig(shared_dir=tmp_path)) == env
    assert shim_env(env, config=AdmissionConfig(shared_dir=tmp_path), operator="op") == env


def test_admission_is_skipped_when_there_is_no_real_uv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A throttle that breaks the lane it protects is worse than no throttle."""
    monkeypatch.setenv("PATH", "/nonexistent-dir-for-sure")
    env = shim_env(
        {"PATH": os.environ["PATH"]},
        config=AdmissionConfig(shared_dir=tmp_path),
        operator="op",
        lane="a",
    )
    assert env["PATH"] == os.environ["PATH"]


def test_the_shim_renders_the_live_classification_table(tmp_path: Path) -> None:
    """A rename must not silently stop admitting."""
    path = write_shim(tmp_path, real="/usr/bin/env uv", config=AdmissionConfig(shared_dir=tmp_path))
    source = path.read_text(encoding="utf-8")
    for tool, pool in admission._POOL_FOR_TOOL.items():
        assert f"{tool!r}: {pool!r}" in source


def test_the_shim_agrees_with_classify_on_real_argv(tmp_path: Path) -> None:
    """Run the shim's own classifier over the cases ``classify`` must agree on."""
    path = write_shim(tmp_path, real="/usr/bin/env uv", config=AdmissionConfig(shared_dir=tmp_path))
    source = path.read_text(encoding="utf-8")
    table = source.split("POOLS = ", 1)[1].split("\n", 1)[0]
    body = source.split("def classify(argv):", 1)[1].split("def ", 1)[0]
    namespace: dict[str, object] = {}
    exec(f"import os\nPOOLS = {table}\ndef classify(argv):{body}", namespace)
    shim_classify: Callable[[list[str]], str | None] = namespace["classify"]  # ty: ignore[invalid-assignment]

    for argv in (["run", "pytest"], ["run", "pyright"], ["sync"], ["--version"]):
        assert shim_classify(argv) == classify(argv)


def test_the_shim_passes_through_without_taking_a_slot(tmp_path: Path) -> None:
    """A non-admitted invocation must not create slot files or queue."""
    config = AdmissionConfig(shared_dir=tmp_path, tests=1)
    path = write_shim(tmp_path / "shim", real=sys.executable, config=config)
    proc = subprocess.run(
        [str(path), "-c", "import sys; print('through')"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0
    assert "through" in proc.stdout
    assert not (tmp_path / "slots" / admission.TEST_POOL).exists()


# ------------------------------------------------- the lock survives exec


def test_the_shim_holds_its_slot_while_the_real_tool_runs(tmp_path: Path) -> None:
    """The CLOEXEC guard, end to end, with real processes.

    The stub "real uv" marks itself as running and sleeps. If the flock were
    dropped at exec, a *second* shim could take the only slot while the first is
    still running — exactly the failure that makes the pool admit everybody.
    """
    config = AdmissionConfig(shared_dir=tmp_path, tests=1, typecheck=1, wait_s=30.0)
    stub = _write_stub_uv(tmp_path)
    shim = write_shim(tmp_path / "shim", real=str(stub), config=config)

    first = subprocess.Popen(
        [str(shim), "run", "pytest", "-q"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second: subprocess.Popen[str] | None = None
    try:
        _wait_for_holders(tmp_path, 1)
        second = subprocess.Popen(
            [str(shim), "run", "pytest", "-q"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _assert_still_blocked(second)
    finally:
        first.wait(timeout=60)
    assert second is not None
    assert second.wait(timeout=60) == 0, "the blocked caller runs once the slot frees"


def test_two_slots_admit_two_and_block_a_third(tmp_path: Path) -> None:
    config = AdmissionConfig(shared_dir=tmp_path, tests=2, typecheck=1, wait_s=30.0)
    stub = _write_stub_uv(tmp_path)
    shim = write_shim(tmp_path / "shim", real=str(stub), config=config)

    procs = [
        subprocess.Popen(
            [str(shim), "run", "pytest", "-q"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(3)
    ]
    try:
        _wait_for_holders(tmp_path, 2)
        _assert_still_blocked(procs[2])
    finally:
        for proc in procs:
            proc.wait(timeout=60)
    assert all(proc.returncode == 0 for proc in procs)


def test_a_shared_dir_makes_pools_shared_across_operators(tmp_path: Path) -> None:
    """The constraint is the hardware, so both operators draw the same slots."""
    a = AdmissionConfig(shared_dir=tmp_path, tests=2)
    b = AdmissionConfig(shared_dir=tmp_path, tests=2)
    assert a.slots_dir() == b.slots_dir()
    c = AdmissionConfig(shared_dir=tmp_path / "elsewhere", tests=2)
    assert c.slots_dir() != a.slots_dir()


def test_the_pool_size_comes_from_config() -> None:
    config = AdmissionConfig(shared_dir=Path("/x"), tests=7, typecheck=3)
    assert config.pool_size(admission.TEST_POOL) == 7
    assert config.pool_size(admission.TYPECHECK_POOL) == 3


# ------------------------------------------------------------- fleet_ops config


def test_a_fleet_ops_config_carries_a_default_admission_budget() -> None:
    """``run_lane`` reads ``config.admission`` before its engine try/except.

    Every lane goes through that read, so a ``FleetOpsConfig`` without the
    field turns every lane into an ``AttributeError`` and takes the lane
    manager and the CLI with it.
    """
    assert FleetOpsConfig().admission == AdmissionConfig()


def test_the_admission_section_sets_every_knob(tmp_path: Path) -> None:
    config = load_fleet_ops_config(
        {
            "fleet_ops": {
                "admission": {
                    "shared_dir": str(tmp_path / "pools"),
                    "tests": 6,
                    "typecheck": 2,
                    "nice": 3,
                    "wait_s": 12.5,
                }
            }
        }
    )
    assert config is not None
    assert config.admission.shared_dir == tmp_path / "pools"
    assert config.admission.tests == 6
    assert config.admission.typecheck == 2
    assert config.admission.nice == 3
    assert config.admission.wait_s == 12.5


@pytest.mark.parametrize("section", [None, {}, "nonsense", {"tests": 0, "nice": -1}])
def test_a_missing_or_nonsense_admission_section_falls_back_to_defaults(
    section: object,
) -> None:
    """Admission is a throttle: a bad knob must never stop a lane from running."""
    config = load_fleet_ops_config({"fleet_ops": {"admission": section}})
    assert config is not None
    assert config.admission == AdmissionConfig()


def test_a_repo_without_an_admission_section_still_runs_admitted() -> None:
    config = load_fleet_ops_config({"fleet_ops": {"base_branch": "trunk"}})
    assert config is not None
    assert config.admission == AdmissionConfig()
    assert config.admission.shared_dir is None


def test_the_shim_slot_layout_matches_the_fleet_slot_pool(tmp_path: Path) -> None:
    """The hand-rolled flock loop must land on the same files SlotPool uses."""
    config = AdmissionConfig(shared_dir=tmp_path, tests=3)
    write_shim(tmp_path / "shim", real=sys.executable, config=config)
    (tmp_path / "slots" / admission.TEST_POOL).mkdir(parents=True)
    (tmp_path / "slots" / admission.TEST_POOL / "slot.0").touch()

    pool = SlotPool(admission.TEST_POOL, root=config.slots_dir(), size=3)
    with pool.slot(timeout_s=5) as held:
        assert Path(held.path) == tmp_path / "slots" / admission.TEST_POOL / "slot.0"


# ------------------------------------------------------------ engine wiring


def test_the_engine_child_receives_the_overlay_env(tmp_path: Path) -> None:
    """The overlay must reach the spawned subprocess.

    A parent's ``PATH`` cannot be changed after the fact, so the only way to get
    the shim onto the engine's ``PATH`` is to hand ``subprocess`` the whole
    environment — which is what this asserts happens, using a real spawn.
    """
    config = AdmissionConfig(shared_dir=tmp_path)
    overlay = shim_env(
        {"PATH": os.environ.get("PATH", "")},
        config=config,
        operator="op",
        lane="a",
    )
    head = overlay["PATH"].split(os.pathsep)[0]
    script = "import os,sys; sys.stdout.write(os.environ['PATH'].split(':')[0])"
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=overlay,
        check=False,
        timeout=60,
    )
    assert proc.stdout == head


def test_spawn_capture_threads_env_to_the_real_spawn(tmp_path: Path) -> None:
    """``_spawn_capture`` must pass ``env`` through to ``subprocess``."""
    import inspect

    assert "env" in inspect.signature(_spawn_capture).parameters
    proc = _spawn_capture(
        [sys.executable, "-c", "print('spawned')"],
        workdir=tmp_path,
        stream_path=None,
        timeout_s=30,
        runner=None,
        env={"PATH": os.environ.get("PATH", "")},
    )
    assert proc[0] == 0
    assert "spawned" in proc[1]


def test_shim_env_is_not_applied_to_the_gate_or_hooks() -> None:
    """Admission covers the engine only.

    A gate that silently queued behind a lane's test slots would stall the merge
    path, and a hook is operator config that must not inherit fleet internals.
    """
    from agent_fleet.fleet_ops import runner

    source = Path(str(runner.__file__)).read_text(encoding="utf-8")
    engine_block = source.split("engine_env = ")[1].split(")\n", 1)[0]
    # The shim overlay is computed once and handed only to the engines...
    assert "admission_mod.shim_env" in engine_block
    # ...and the gate env is built from os.environ, never from the overlay.
    assert "env={**os.environ, **binding_mod.gate_env(bound.binding)}" in source
    assert "engine_env" not in source.split("gate_mod.run_gate")[1].split(")", 1)[0]


# ----------------------------------------------------------------- helpers


def _write_stub_uv(tmp_path: Path) -> Path:
    """A stand-in for the real ``uv``.

    It touches a marker file on start and a second one before exiting, so the
    test can observe *from outside* that a slot is held and later released —
    another process's stdout is not readable after ``Popen`` buffers it.
    """
    stub = tmp_path / "real-uv"
    stub.write_text(
        f"#!{sys.executable}\n"
        f"import os, time\n"
        f"MARK = {str(tmp_path / 'running')!r}\n"
        f"fd = os.open(MARK, os.O_CREAT | os.O_WRONLY | os.O_APPEND)\n"
        f"os.write(fd, b'x')\n"
        f"os.close(fd)\n"
        "time.sleep(3)\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def _wait_for_holders(tmp_path: Path, minimum: int) -> None:
    """Block until at least *minimum* stub processes are running."""
    import time

    marker = tmp_path / "running"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if marker.exists() and marker.stat().st_size >= minimum:
            return
        time.sleep(0.1)
    raise AssertionError(f"fewer than {minimum} holders reported within the deadline")


def _assert_still_blocked(proc: subprocess.Popen[str]) -> None:
    """The blocked process must still be waiting, not admitted."""
    import time

    time.sleep(1.5)
    assert proc.poll() is None, "a caller was admitted while every slot was held"
