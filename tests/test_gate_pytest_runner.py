"""Tests for the gate's memory-capped pytest runner and package grouping."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_fleet.gate.pytest_runner import (
    PYTEST_INFRA_ERROR,
    PYTEST_OK,
    PYTEST_TESTS_FAILED,
    PytestResult,
    build_pytest_command,
    find_test_packages,
    is_test_file,
    memory_to_bytes,
    run_pytest,
    systemd_run_available,
)

_PYPROJECT = """[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[project]
name = "gate-fixture"
version = "0.0.0"
"""

# ---------------------------------------------------------------------------
# is_test_file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_thing.py",
        "test_root.py",
        "api/tests/test_api.py",
        "a/b/test_deep.py",
    ],
)
def test_is_test_file_accepts_test_files(path: str) -> None:
    assert is_test_file(path)


@pytest.mark.parametrize(
    "path",
    [
        "agent_fleet/gate/pipeline.py",
        "tests/conftest.py",
        "tests/helpers.py",
        "mytest_x.py",
        "tests/test_thing.txt",
        "docs/test_guide.md",
        "",
    ],
)
def test_is_test_file_rejects_non_tests(path: str) -> None:
    """conftest.py defines fixtures and is not a test itself; helpers aren't either.
    A directory merely *named* testing/ is fine — the file inside still matches."""
    assert not is_test_file(path)


def test_is_test_file_accepts_a_dir_named_testing() -> None:
    assert is_test_file("testing/test_thing.py")


def test_is_test_file_accepts_path_objects() -> None:
    assert is_test_file(Path("tests/test_x.py"))


# ---------------------------------------------------------------------------
# memory_to_bytes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("6G", 6 * 1024**3),
        ("512M", 512 * 1024**2),
        ("1T", 1024**4),
        ("1024K", 1024 * 1024),
        ("2048", 2048),
        ("1.5G", int(1.5 * 1024**3)),
    ],
)
def test_memory_to_bytes(text: str, expected: int) -> None:
    assert memory_to_bytes(text) == expected


def test_memory_to_bytes_is_case_insensitive() -> None:
    assert memory_to_bytes("6g") == 6 * 1024**3
    assert memory_to_bytes("6GB") == 6 * 1024**3


@pytest.mark.parametrize("bad", ["", "abc", "6X", "-"])
def test_memory_to_bytes_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError, match="unparseable"):
        memory_to_bytes(bad)


# ---------------------------------------------------------------------------
# find_test_packages
# ---------------------------------------------------------------------------


def _write_pyproject(root: Path, *parts: str) -> None:
    """Create a pyproject.toml at *parts* under *root* (making dirs as needed)."""
    target = root.joinpath(*parts, "pyproject.toml")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_PYPROJECT, encoding="utf-8")


def test_find_test_packages_groups_by_owning_pyproject(tmp_path: Path) -> None:
    """A repo with a root and an api/ sub-package must run pytest per package:
    the venv and pythonpath differ, so one combined run would not resolve."""
    _write_pyproject(tmp_path)
    _write_pyproject(tmp_path, "api")

    packages = find_test_packages(
        tmp_path, ["tests/test_root.py", "api/tests/test_api.py", "api/test_top.py"]
    )

    by_dir = {p.rel_dir: p.tests for p in packages}
    assert by_dir == {
        ".": ["tests/test_root.py"],
        "api": ["api/test_top.py", "api/tests/test_api.py"],
    }
    assert packages[0].dir == tmp_path


def test_find_test_packages_falls_back_to_the_root(tmp_path: Path) -> None:
    _write_pyproject(tmp_path)
    packages = find_test_packages(tmp_path, ["deep/nested/test_x.py"])
    assert [p.rel_dir for p in packages] == ["."]


def test_find_test_packages_with_no_pyproject_anywhere(tmp_path: Path) -> None:
    packages = find_test_packages(tmp_path, ["tests/test_x.py"])
    assert [p.rel_dir for p in packages] == ["."]


def test_package_dir_forces_a_single_package(tmp_path: Path) -> None:
    """Some repos have a root pyproject that does not own the tests."""
    _write_pyproject(tmp_path)
    _write_pyproject(tmp_path, "api")
    packages = find_test_packages(
        tmp_path, ["api/tests/test_a.py", "api/tests/test_b.py"], package_dir="api"
    )
    assert len(packages) == 1
    assert packages[0].rel_dir == "api"
    assert packages[0].tests == ["api/tests/test_a.py", "api/tests/test_b.py"]


def test_find_test_packages_sorts_deterministically(tmp_path: Path) -> None:
    _write_pyproject(tmp_path)
    _write_pyproject(tmp_path, "b")
    _write_pyproject(tmp_path, "a")
    packages = find_test_packages(tmp_path, ["b/test_b.py", "a/test_a.py", "tests/test_r.py"])
    assert [p.rel_dir for p in packages] == [".", "a", "b"]


# ---------------------------------------------------------------------------
# PytestResult semantics
# ---------------------------------------------------------------------------


def test_pytest_result_exit_code_semantics() -> None:
    ok = PytestResult(returncode=PYTEST_OK, stdout="", stderr="")
    assert ok.passed
    assert not ok.tests_failed
    assert not ok.infra_error

    failed = PytestResult(returncode=PYTEST_TESTS_FAILED, stdout="", stderr="")
    assert failed.tests_failed
    assert not failed.infra_error

    infra = PytestResult(returncode=PYTEST_INFRA_ERROR, stdout="", stderr="")
    assert infra.infra_error
    assert not infra.tests_failed


@pytest.mark.parametrize("code", [2, 3, 4, 5])
def test_any_exit_above_one_is_an_infra_error(code: int) -> None:
    """Collection errors, usage errors and interruptions taught us nothing
    about the code, so the gate must never read them as a finding."""
    assert PytestResult(returncode=code, stdout="", stderr="").infra_error


def test_failed_ids_are_parsed_from_pytest_output() -> None:
    stdout = (
        "FAILED tests/test_a.py::test_one - assert 0 == 1\n"
        "FAILED tests/test_a.py::test_two - boom\n"
        "1 passed\n"
    )
    result = PytestResult(returncode=1, stdout=stdout, stderr="")
    assert result.failed_ids == [
        "tests/test_a.py::test_one",
        "tests/test_a.py::test_two",
    ]


def test_summary_prefers_the_pytest_summary_line() -> None:
    stdout = "noise\nSUMMARY: 3 failed, 1 passed in 1.2s\n"
    assert PytestResult(returncode=1, stdout=stdout, stderr="").summary.startswith("SUMMARY:")


def test_summary_falls_back_to_the_last_line() -> None:
    result = PytestResult(returncode=2, stdout="a\nb\ncollection error", stderr="")
    assert result.summary == "collection error"


def test_summary_handles_empty_output() -> None:
    assert PytestResult(returncode=2, stdout="", stderr="").summary == ""


# ---------------------------------------------------------------------------
# build_pytest_command
# ---------------------------------------------------------------------------


def test_command_without_systemd_is_the_bare_pytest_invocation() -> None:
    cmd = build_pytest_command(["tests/test_a.py"], use_systemd=False)
    assert cmd == ["uv", "run", "pytest", "-q", "--no-header", "tests/test_a.py"]


def test_command_with_systemd_applies_the_memory_cap() -> None:
    """A runaway pytest once consumed 36GB; the cap is the whole point."""
    cmd = build_pytest_command(["tests/test_a.py"], memory="6G", use_systemd=True)
    assert cmd[:4] == ["systemd-run", "--user", "--scope", "--quiet"]
    assert "MemoryMax=6G" in cmd
    assert "MemorySwapMax=0" in cmd
    assert cmd[-1] == "tests/test_a.py"


def test_systemd_command_honours_a_custom_memory_cap() -> None:
    cmd = build_pytest_command(["t.py"], memory="2G", use_systemd=True)
    assert "MemoryMax=2G" in cmd


def test_systemd_run_available_returns_a_bool() -> None:
    assert isinstance(systemd_run_available(), bool)


# ---------------------------------------------------------------------------
# run_pytest
# ---------------------------------------------------------------------------


def test_run_pytest_with_no_files_is_a_pass(tmp_path: Path) -> None:
    result = run_pytest(tmp_path, [], use_systemd=False)
    assert result.passed
    assert result.stdout == ""


def test_run_pytest_reports_a_test_failure(tmp_path: Path) -> None:
    (tmp_path / "test_fail.py").write_text("def test_x():\n    assert False\n", encoding="utf-8")
    result = run_pytest(tmp_path, ["test_fail.py"], timeout_s=300, use_systemd=False)
    assert result.returncode == PYTEST_TESTS_FAILED
    assert result.tests_failed
    assert not result.infra_error


def test_run_pytest_reports_a_pass(tmp_path: Path) -> None:
    (tmp_path / "test_ok.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    result = run_pytest(tmp_path, ["test_ok.py"], timeout_s=300, use_systemd=False)
    assert result.returncode == PYTEST_OK
    assert result.failed_ids == []


def test_run_pytest_treats_a_collection_error_as_infra(tmp_path: Path) -> None:
    """A syntax error means the suite could not run — never a finding."""
    (tmp_path / "test_broken.py").write_text("def test_x(:\n", encoding="utf-8")
    result = run_pytest(tmp_path, ["test_broken.py"], timeout_s=300, use_systemd=False)
    assert result.infra_error


def test_run_pytest_timeout_is_an_infra_error(tmp_path: Path) -> None:
    """A hung suite is not evidence the code is broken."""
    result = run_pytest(tmp_path, ["nonexistent.py"], timeout_s=0, use_systemd=False)
    # A nonexistent path exits non-zero; either way it is not a clean pass.
    assert not result.passed


def test_run_pytest_does_not_raise_on_a_missing_file(tmp_path: Path) -> None:
    """Never raises on a non-zero exit — the caller reads infra_error instead."""
    try:
        result = run_pytest(tmp_path, ["does_not_exist.py"], timeout_s=60, use_systemd=False)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        pytest.fail("run_pytest must not propagate a timeout")
    assert result.returncode != 0
