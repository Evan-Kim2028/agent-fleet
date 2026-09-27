from __future__ import annotations

from typing import TYPE_CHECKING

from agent_fleet.fleet_ops import memcap

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _cg(tmp_path: Path, text: str) -> str:
    f = tmp_path / "cgroup"
    f.write_text(text, encoding="utf-8")
    return str(f)


def test_scope_joins_the_callers_own_slice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(memcap.SCOPE_SLICE_ENV, raising=False)
    path = _cg(
        tmp_path,
        "0::/user.slice/user-1000.slice/user@1000.service/fleet.slice/fleet-lane-x.service\n",
    )
    assert memcap.scope_slice(path) == "fleet.slice"


def test_no_slice_outside_a_user_slice_keeps_systemd_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(memcap.SCOPE_SLICE_ENV, raising=False)
    path = _cg(tmp_path, "0::/user.slice/user-1000.slice/session-3.scope\n")
    assert memcap.scope_slice(path) is None


def test_env_override_wins_and_empty_means_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _cg(tmp_path, "0::/user.slice/user-1000.slice/user@1000.service/fleet.slice/x.service\n")
    monkeypatch.setenv(memcap.SCOPE_SLICE_ENV, "agents.slice")
    assert memcap.scope_slice(path) == "agents.slice"
    monkeypatch.setenv(memcap.SCOPE_SLICE_ENV, "")
    assert memcap.scope_slice(path) is None


def test_plan_passes_the_slice_to_systemd_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(memcap.SCOPE_SLICE_ENV, "fleet.slice")
    monkeypatch.setattr(memcap.shutil, "which", lambda name: "/usr/bin/" + name)
    plan = memcap.plan_memory_cap(["echo", "hi"], use_systemd=True)
    assert "--slice=fleet.slice" in plan.argv
    assert plan.argv.index("--slice=fleet.slice") < plan.argv.index("echo")
