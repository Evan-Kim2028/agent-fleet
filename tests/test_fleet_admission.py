from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

OPS_VPS = Path(__file__).resolve().parent.parent / "ops" / "vps"
HELPER = OPS_VPS / "worker" / "bin" / "fleet_admission.py"
GIB = 1024**3


def _run_capacity(
    tmp_path: Path, host_gib: int, current_gib: int, max_gib: int | str
) -> tuple[int, dict[str, object]]:
    cgroup = tmp_path / "fleet.slice"
    cgroup.mkdir()
    (cgroup / "memory.current").write_text(str(current_gib * GIB))
    (cgroup / "memory.max").write_text(str(max_gib * GIB if isinstance(max_gib, int) else max_gib))
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(f"MemAvailable: {host_gib * GIB // 1024} kB\n")
    result = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "--fleet-cgroup",
            str(cgroup),
            "--meminfo",
            str(meminfo),
            "--report",
            "--check",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    return result.returncode, json.loads(result.stdout)


def test_every_pytest_entrypoint_uses_the_one_shared_slot() -> None:
    from agent_fleet.fleet_ops.admission import TEST_POOL, AdmissionConfig
    from agent_fleet.gate.config import GateConfig
    from agent_fleet.slots import DEFAULT_TEST_POOL_SIZE

    assert TEST_POOL == "test"
    assert AdmissionConfig().tests == 1
    assert GateConfig().test_slots == DEFAULT_TEST_POOL_SIZE == 1
    worker_shim = (OPS_VPS / "worker" / "fb" / "shim" / "uv").read_text()
    orchestrator_shim = (OPS_VPS / "orchestrator" / "shim" / "uv").read_text()
    gate_pytest = (OPS_VPS / "orchestrator" / "fm_pytest.sh").read_text()
    driver = (OPS_VPS / "worker" / "bin" / "gate_queue_run.sh").read_text()
    rebase = (OPS_VPS / "worker" / "bin" / "lane_rebase.sh").read_text()
    fastmerge = (OPS_VPS / "orchestrator" / "fastmerge_ext.sh").read_text()
    worker_gate = (OPS_VPS / "worker" / "fb" / "fbgate").read_text()
    orchestrator_gate = (OPS_VPS / "orchestrator" / "fbgate").read_text()
    lane_runner = (OPS_VPS / "worker" / "bin" / "lane_impl.sh").read_text()
    service = (OPS_VPS / "systemd" / "fleet-gate-queue.service").read_text()
    timer = (OPS_VPS / "systemd" / "fleet-gate-queue.timer").read_text()
    train_service = (OPS_VPS / "systemd" / "fleet-merge-train.service").read_text()
    for source in (worker_shim, orchestrator_shim):
        assert "POOL=test; N=1" in source
        assert "/slots/test" in source
        assert "timeout --signal=TERM --kill-after=10s" in source
        assert "--fleet-headroom-gib 6" in source
    assert "/slots/test" in gate_pytest
    assert "slot.$i" in gate_pytest
    assert "FLEET_TEST_SLOT_DIR" not in gate_pytest
    assert "--fleet-headroom-gib 6" in gate_pytest
    assert "python -m pytest" in gate_pytest
    assert 'PYTHONPATH="$d:$ROOT' in gate_pytest
    assert all("FLEET_TEST_SLOT_DIR" not in source for source in (worker_shim, orchestrator_shim))
    assert "FLEET_CAPACITY_HELPER" in lane_runner
    assert "--check" in driver and "gate_processes" in driver and "sort -u" in driver
    assert "changed_tests=" in fastmerge and "covered=1" in fastmerge
    assert "*'merge conflict'*" in driver and "*'conflict with main'*" in driver
    assert "normalize_queue" in driver and "canonical_lane" in driver
    assert "QUEUE_SCAN_ONCE" in driver
    assert "merge-train regression" in driver and "STUCK_GATE_S" in driver
    assert "STUCK_REBASE_S" in driver and "rebase agent did not push" in driver
    assert "slots/gate" in worker_gate and "gate_live" in worker_gate
    assert "slots/gate" in orchestrator_gate and "gate_live" in orchestrator_gate
    assert "gate-$LANE.lock" in worker_gate and "gate-$LANE.lock" in orchestrator_gate
    sweep = (OPS_VPS / "worker" / "bin" / "merge_train_sweep.sh").read_text()
    assert all(repo in sweep for repo in ("lake-of-rage", "silphcoanalytics", "agent-fleet"))
    assert "--status-dir" in sweep and "--include-head-prefix" in sweep
    assert "headRefName" in rebase and "park_worktree" in rebase
    assert "symbolic-ref -d HEAD" in rebase
    assert "--force-with-lease" in rebase
    assert "DGRH" not in driver and "circuit(){" not in driver
    assert "Restart=on-failure" in service and "%h/fleet/bin/gate_queue_run.sh" in service
    assert "%h/fleet/bin/merge_train_sweep.sh" in train_service
    assert "OnUnitInactiveSec=30s" in timer


def test_capacity_opens_with_host_and_fleet_headroom(tmp_path: Path) -> None:
    rc, decision = _run_capacity(tmp_path, 39, 9, 15)
    assert rc == 0
    assert decision["admitted"] is True
    assert decision["reason"] == "capacity_available"
    assert decision["fleet_headroom_bytes"] == 6 * GIB


def test_host_reserve_closes_admission_despite_fleet_headroom(tmp_path: Path) -> None:
    rc, decision = _run_capacity(tmp_path, 15, 2, 15)
    assert rc == 1
    assert decision["reason"] == "host_reserve"


def test_fleet_headroom_closes_admission_despite_host_memory(tmp_path: Path) -> None:
    rc, decision = _run_capacity(tmp_path, 39, 14, 15)
    assert rc == 1
    assert decision["reason"] == "fleet_headroom"


def test_capacity_helper_compiles_with_system_python() -> None:
    result = subprocess.run(
        [
            "python3",
            "-c",
            "import pathlib, sys; "
            "p=pathlib.Path(sys.argv[1]); compile(p.read_text(), str(p), 'exec')",
            str(HELPER),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr


def test_unknown_cgroup_limit_fails_closed(tmp_path: Path) -> None:
    rc, decision = _run_capacity(tmp_path, 39, 2, "max")
    assert rc == 1
    assert decision["reason"] == "capacity_unknown"
