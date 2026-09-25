"""The memory cap and the targeted-test scoping.

The cap exists because of a 36 GB runaway pytest on this machine; the scoping
exists because the lake-of-rage pipe suite alone runs for 83 minutes. Both are
fences (owner, 2026-09-24/25), so the tests assert the fence itself, not just
that the helper runs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import overload

import pytest

from agent_fleet.fleet_ops import memcap
from agent_fleet.fleet_ops.memcap import (
    DEFAULT_MEMORY_MAX,
    DEVIN_MEMORY_MAX,
    MemoryCapError,
    _ulimit_kb,
    changed_files,
    discover_test_files,
    plan_memory_cap,
    scope_tests,
)

# ----------------------------------------------------------------- the cap


def test_default_cap_is_the_mandated_6g() -> None:
    assert DEFAULT_MEMORY_MAX == "6G"
    assert DEVIN_MEMORY_MAX == "10G"


def test_systemd_plan_sets_both_memory_properties() -> None:
    plan = plan_memory_cap(["pytest", "-q", "tests/x.py"], use_systemd=True)
    assert plan.mechanism == "systemd"
    joined = " ".join(plan.argv)
    assert "MemoryMax=6G" in joined
    # No swap: a swapped-out process defeats the cap entirely.
    assert "MemorySwapMax=0" in joined
    assert plan.argv[-3:] == ["pytest", "-q", "tests/x.py"]


def test_systemd_uses_the_user_scope() -> None:
    plan = plan_memory_cap(["cmd"], use_systemd=True)
    assert plan.argv[:3] == ["systemd-run", "--user", "--scope"]


def test_ulimit_fallback_when_systemd_is_unavailable() -> None:
    plan = plan_memory_cap(["pytest", "-q"], use_systemd=False)
    assert plan.mechanism == "ulimit"
    assert plan.argv[0] == "sh"
    assert "ulimit -v" in plan.argv[2]
    assert "exec pytest -q" in plan.argv[2]


def test_the_caller_argv_is_shell_quoted() -> None:
    """A task file with a space in its name must not split into two arguments."""
    plan = plan_memory_cap(["cmd", "-p", "a b.md"], use_systemd=False)
    assert "'a b.md'" in plan.argv[2]


@pytest.mark.parametrize(
    ("spec", "expected_kb"),
    [
        ("6G", 6 * 1024 * 1024),
        ("6144M", 6 * 1024 * 1024),
        ("6291456K", 6291456),
        ("1048576", 1048576),
    ],
)
def test_ulimit_unit_conversion(spec: str, expected_kb: int) -> None:
    assert _ulimit_kb(spec) == expected_kb


def test_an_empty_cap_is_an_error() -> None:
    with pytest.raises(MemoryCapError):
        _ulimit_kb("")
    with pytest.raises(MemoryCapError):
        _ulimit_kb("nonsense")


def test_a_nonpositive_cap_is_an_error() -> None:
    with pytest.raises(MemoryCapError, match="positive"):
        _ulimit_kb("0G")


def test_capping_an_empty_command_is_an_error() -> None:
    with pytest.raises(ValueError, match="empty command"):
        plan_memory_cap([], use_systemd=False)


def test_systemd_requested_but_missing_raises_rather_than_running_uncapped(monkeypatch) -> None:  # noqa: ANN001
    """Never silently run unbounded — a visible failure is the correct outcome."""
    import shutil

    real = shutil.which

    @overload
    def fake_which(cmd: str, mode: int = 1, path: str | None = None) -> str | None: ...

    @overload
    def fake_which(cmd: bytes, mode: int = 1, path: str | None = None) -> bytes | None: ...

    def fake_which(
        cmd: str | bytes,
        mode: int = 1,
        path: str | None = None,
    ) -> str | bytes | None:
        if cmd == "systemd-run":
            return None
        return real(cmd, mode, path)

    import agent_fleet.fleet_ops.memcap as memcap_mod

    monkeypatch.setattr(memcap_mod.shutil, "which", fake_which)
    with pytest.raises(MemoryCapError, match="not on PATH"):
        plan_memory_cap(["pytest"], use_systemd=True)


# --------------------------------------------------------------- test scoping


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pipe" / "catalog").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pipe" / "catalog" / "en_jumbo_metal_mint.py").write_text("x = 1\n", encoding="utf-8")
    (root / "tests" / "test_en_jumbo_metal_mint.py").write_text(
        "def test_x(): pass\n", encoding="utf-8"
    )
    (root / "tests" / "test_unrelated.py").write_text("def test_y(): pass\n", encoding="utf-8")
    return root


def test_changed_files_includes_uncommitted_work(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    def runner(args, **_kwargs: object):  # noqa: ANN001, ANN202
        argv = list(args)
        if "--others" in argv:
            return subprocess.CompletedProcess(argv, 0, "new_file.py\n", "")
        if "ls-files" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "--name-only" in argv and len(argv) > 3:
            return subprocess.CompletedProcess(argv, 0, "committed.py\n", "")
        return subprocess.CompletedProcess(argv, 0, "committed.py\n", "")

    files = {p.name for p in changed_files(root, base="main", runner=runner)}
    # A file the agent committed, and one it left uncommitted, are both in scope.
    assert {"committed.py", "new_file.py"} <= files


def test_scope_selects_the_matching_test_file(repo: Path) -> None:
    def runner(args, **_kwargs: object):  # noqa: ANN001, ANN202
        argv = list(args)
        if "ls-files" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "pipe/catalog/en_jumbo_metal_mint.py\n", "")

    scope = scope_tests(repo, base="main", test_roots=[repo / "tests"], runner=runner)
    assert [p.name for p in scope.test_files] == ["test_en_jumbo_metal_mint.py"]


def test_scope_selects_tests_that_import_the_changed_module(repo: Path) -> None:
    """A refactor of a shared helper breaks its consumers' tests too."""
    (repo / "tests" / "test_consumer.py").write_text(
        "from pipe.catalog import en_jumbo_metal_mint\n", encoding="utf-8"
    )

    def runner(args, **_kwargs: object):  # noqa: ANN001, ANN202
        argv = list(args)
        if "ls-files" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "pipe/catalog/en_jumbo_metal_mint.py\n", "")

    scope = scope_tests(repo, base="main", test_roots=[repo / "tests"], runner=runner)
    names = {p.name for p in scope.test_files}
    assert "test_consumer.py" in names
    assert "test_unrelated.py" not in names


def test_an_empty_scope_runs_nothing_rather_than_the_whole_suite(repo: Path) -> None:
    """A full-suite run is the 36 GB failure this scoping exists to prevent."""

    def runner(args, **_kwargs: object):  # noqa: ANN001, ANN202
        return subprocess.CompletedProcess(list(args), 0, "", "")

    scope = scope_tests(repo, base="main", test_roots=[repo / "tests"], runner=runner)
    assert scope.empty
    assert scope.argv() == ["pytest"]


def test_argv_lists_the_selected_files() -> None:
    scope = memcap.TestScope(test_files=(Path("a.py"), Path("b.py")), changed_files=())
    assert scope.argv() == ["pytest", "a.py", "b.py"]


def test_discovering_from_a_missing_root_is_not_an_error(tmp_path: Path) -> None:
    assert discover_test_files([tmp_path / "nope"]) == []
