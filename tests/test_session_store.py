"""Tests for agent_fleet.session_store — worktree-keyed durable session id persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_fleet.session_store import clear_session_id, load_session_id, persist_session_id

if TYPE_CHECKING:
    from pathlib import Path


def test_persist_then_load_round_trip(tmp_path: Path) -> None:
    store = tmp_path / "store"
    worktree = "/tmp/agent-fleet-worktrees/run-abc"
    persist_session_id(worktree, "devin-session-1", store_dir=store)
    assert load_session_id(worktree, store_dir=store) == "devin-session-1"


def test_load_missing_worktree_returns_none(tmp_path: Path) -> None:
    store = tmp_path / "store"
    assert load_session_id("/nonexistent/worktree", store_dir=store) is None


def test_persist_overwrites_previous_session_id(tmp_path: Path) -> None:
    store = tmp_path / "store"
    worktree = "/tmp/agent-fleet-worktrees/run-abc"
    persist_session_id(worktree, "old-session", store_dir=store)
    persist_session_id(worktree, "new-session", store_dir=store)
    assert load_session_id(worktree, store_dir=store) == "new-session"


def test_different_worktrees_do_not_collide(tmp_path: Path) -> None:
    store = tmp_path / "store"
    persist_session_id("/tmp/wt-a", "session-a", store_dir=store)
    persist_session_id("/tmp/wt-b", "session-b", store_dir=store)
    assert load_session_id("/tmp/wt-a", store_dir=store) == "session-a"
    assert load_session_id("/tmp/wt-b", store_dir=store) == "session-b"


def test_clear_session_id_removes_entry(tmp_path: Path) -> None:
    store = tmp_path / "store"
    worktree = "/tmp/agent-fleet-worktrees/run-abc"
    persist_session_id(worktree, "devin-session-1", store_dir=store)
    clear_session_id(worktree, store_dir=store)
    assert load_session_id(worktree, store_dir=store) is None


def test_clear_missing_entry_does_not_raise(tmp_path: Path) -> None:
    store = tmp_path / "store"
    clear_session_id("/never/persisted", store_dir=store)  # must not raise


def test_persist_empty_session_id_is_a_noop(tmp_path: Path) -> None:
    store = tmp_path / "store"
    persist_session_id("/tmp/wt", "", store_dir=store)
    assert load_session_id("/tmp/wt", store_dir=store) is None


def test_load_malformed_json_returns_none(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir(parents=True)
    from agent_fleet.session_store import _key_path

    _key_path("/tmp/wt", store_dir=store).write_text("not json", encoding="utf-8")
    assert load_session_id("/tmp/wt", store_dir=store) is None
