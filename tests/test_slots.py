"""Tests for agent_fleet.slots — the machine-wide cross-process slot pools."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agent_fleet.slots import (
    DEFAULT_POOL_SIZE,
    PoolConfig,
    SlotPool,
    SlotPoolFull,
    agent_slot_pool,
    declared_size,
    record_size,
)
from agent_fleet.slots import test_slot_pool as make_test_pool


def test_acquire_and_release_is_reusable(tmp_path: Path) -> None:
    pool = SlotPool("agent", root=tmp_path, size=2)
    with pool.slot() as first:
        assert first.path.is_file()
    with pool.slot() as second:
        assert second.path == first.path  # the same slot is reusable


def test_slots_are_exclusive_within_a_process(tmp_path: Path) -> None:
    """A second acquire must not hand out a slot that is already held."""
    pool = SlotPool("agent", root=tmp_path, size=1)
    held = pool.acquire()
    try:
        # flock is per open-file-description, so re-acquiring the only slot
        # cannot succeed from this process while it is held.
        with pytest.raises(SlotPoolFull):
            pool.acquire(timeout_s=0.1)
    finally:
        pool.release(held)


def test_acquire_times_out_when_pool_is_full(tmp_path: Path) -> None:
    pool = SlotPool("agent", root=tmp_path, size=1)
    held = pool.acquire()
    try:
        with pytest.raises(SlotPoolFull) as exc:
            pool.acquire(timeout_s=0.05)
        assert "agent" in str(exc.value)
    finally:
        pool.release(held)


def test_release_is_idempotent(tmp_path: Path) -> None:
    pool = SlotPool("agent", root=tmp_path, size=1)
    held = pool.acquire()
    pool.release(held)
    pool.release(held)  # must not raise


def test_in_use_counts_held_slots(tmp_path: Path) -> None:
    pool = SlotPool("agent", root=tmp_path, size=3)
    assert pool.in_use() == 0
    with pool.slot():
        assert pool.in_use() == 1
    assert pool.in_use() == 0


def test_size_falls_back_to_declared_then_default(tmp_path: Path) -> None:
    pool = SlotPool("agent", root=tmp_path)
    assert pool.size == DEFAULT_POOL_SIZE
    record_size(tmp_path, "agent", 7)
    assert declared_size(tmp_path, "agent") == 7
    assert SlotPool("agent", root=tmp_path).size == 7


def test_explicit_size_overrides_declared(tmp_path: Path) -> None:
    record_size(tmp_path, "agent", 7)
    assert SlotPool("agent", root=tmp_path, size=3).size == 3


def test_record_size_rejects_nonpositive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive"):
        record_size(tmp_path, "agent", 0)


def test_record_size_tolerates_corrupt_record(tmp_path: Path) -> None:
    path = tmp_path / "agent" / "pool.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert declared_size(tmp_path, "agent") is None


def test_acquire_records_the_pool_size(tmp_path: Path) -> None:
    pool = SlotPool("agent", root=tmp_path, size=5)
    with pool.slot():
        assert declared_size(tmp_path, "agent") == 5


def test_pool_dir_layout(tmp_path: Path) -> None:
    pool = SlotPool("agent", root=tmp_path, size=2)
    with pool.slot() as held:
        assert held.path.parent == tmp_path / "agent"
        assert held.path.name == "slot.0"


def test_agent_and_test_pools_are_separate(tmp_path: Path) -> None:
    cfg = PoolConfig(root=tmp_path, agent_slots=5, test_slots=2)
    assert agent_slot_pool(cfg).name == "agent"
    assert agent_slot_pool(cfg).size == 5
    assert make_test_pool(cfg).name == "test"
    assert make_test_pool(cfg).size == 2


def test_default_test_pool_is_smaller_than_agent_pool(tmp_path: Path) -> None:
    """Test runs are the memory hogs, so their pool must bind first."""
    cfg = PoolConfig(root=tmp_path, agent_slots=24, test_slots=4)
    assert make_test_pool(cfg).size < agent_slot_pool(cfg).size


_SUBPROCESS_HOLDER = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, {repo!r})
    from agent_fleet.slots import SlotPool
    pool = SlotPool("agent", root={root!r}, size=1)
    held = pool.acquire()
    print("held", flush=True)
    time.sleep(30)
    """
)


def test_slot_is_released_when_the_holding_process_dies(tmp_path: Path) -> None:
    """A crashed process must not leak a slot: the kernel drops its flock."""
    repo = str(Path(__file__).resolve().parent.parent)
    script = _SUBPROCESS_HOLDER.format(repo=repo, root=str(tmp_path))
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "held"
        # The slot is held by the child; this process must not get it.
        pool = SlotPool("agent", root=tmp_path, size=1)
        with pytest.raises(SlotPoolFull):
            pool.acquire(timeout_s=0.2)
        proc.kill()
        proc.wait(timeout=30)
        # Once the child is gone the kernel has released the lock.
        with pool.slot(timeout_s=5):
            pass
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=30)


def test_slot_file_is_created_with_owner_only_permissions(tmp_path: Path) -> None:
    pool = SlotPool("agent", root=tmp_path, size=1)
    with pool.slot() as held:
        mode = held.path.stat().st_mode & 0o777
        assert mode == 0o600
        assert os.access(held.path, os.R_OK)
