"""Tests for agent_fleet.doctor preflight checks."""

from __future__ import annotations

from typing import TYPE_CHECKING

import agent_fleet.doctor as doctor
from agent_fleet.doctor import DoctorCheck, doctor_exit_code, render_doctor, run_doctor_checks

if TYPE_CHECKING:
    import pytest


# --- (a) backend key ---


def test_cursor_api_key_pass_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "k")
    checks = run_doctor_checks(backend="cursor")
    match = next(c for c in checks if c.name == "CURSOR_API_KEY")
    assert match.status == "pass"
    assert match.detail == "set"


def test_cursor_api_key_fail_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    checks = run_doctor_checks(backend="cursor")
    match = next(c for c in checks if c.name == "CURSOR_API_KEY")
    assert match.status == "fail"
    assert match.fix != ""


# --- (b) kimi backend ---


def test_kimi_backend_includes_kimi_api_key_check() -> None:
    checks = run_doctor_checks(backend="kimi")
    names = [c.name for c in checks]
    assert "KIMI_API_KEY" in names


# --- (c) unknown backend ---


def test_unknown_backend_yields_warn_with_unknown_detail() -> None:
    checks = run_doctor_checks(backend="zzz")
    match = next((c for c in checks if c.status == "warn" and "unknown backend" in c.detail), None)
    assert match is not None


# --- (d) repo_present ---


def test_repo_present_none_yields_no_repo_config_check() -> None:
    checks = run_doctor_checks(repo_present=None)
    assert not any(c.name == "repo config" for c in checks)


def test_repo_present_true_yields_pass() -> None:
    checks = run_doctor_checks(repo_present=True)
    match = next(c for c in checks if c.name == "repo config")
    assert match.status == "pass"


def test_repo_present_false_yields_warn() -> None:
    checks = run_doctor_checks(repo_present=False)
    match = next(c for c in checks if c.name == "repo config")
    assert match.status == "warn"


# --- (e) doctor_exit_code ---


def test_exit_code_1_when_any_fail() -> None:
    checks = [
        DoctorCheck("a", "pass", "ok"),
        DoctorCheck("b", "fail", "bad", "fix it"),
        DoctorCheck("c", "warn", "meh"),
    ]
    assert doctor_exit_code(checks) == 1


def test_exit_code_0_when_all_pass() -> None:
    checks = [DoctorCheck("a", "pass", "ok"), DoctorCheck("b", "pass", "good")]
    assert doctor_exit_code(checks) == 0


def test_exit_code_0_when_all_warn() -> None:
    checks = [DoctorCheck("a", "warn", "meh"), DoctorCheck("b", "warn", "also meh")]
    assert doctor_exit_code(checks) == 0


# --- (f) python check ---


def test_python_runtime_check_passes_on_current_interpreter() -> None:
    checks = run_doctor_checks()
    match = next(c for c in checks if c.name == "Python runtime")
    assert match.status == "pass"


# --- (g) render_doctor ---


def test_render_doctor_contains_header() -> None:
    checks = [DoctorCheck("X", "pass", "ok")]
    out = render_doctor(checks)
    assert "agent-fleet doctor" in out


def test_render_doctor_footer_shows_counts() -> None:
    checks = [DoctorCheck("X", "pass", "ok")]
    out = render_doctor(checks)
    assert "0 failed" in out


def test_render_doctor_shows_fix_for_non_pass() -> None:
    checks = [DoctorCheck("bad", "fail", "oops", "run something")]
    out = render_doctor(checks)
    assert "fix:" in out
    assert "run something" in out


def test_doctor_check_to_dict_roundtrips() -> None:
    c = DoctorCheck("my-check", "warn", "some detail", "do the thing")
    d = c.to_dict()
    assert d["name"] == "my-check"
    assert d["status"] == "warn"
    assert d["detail"] == "some detail"
    assert d["fix"] == "do the thing"


# --- gh CLI (deterministic monkeypatch) ---


def test_gh_check_warn_not_installed_when_which_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    checks = run_doctor_checks()
    match = next(c for c in checks if c.name == "GitHub CLI (gh)")
    assert match.status == "warn"
    assert "not installed" in match.detail
