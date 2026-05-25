"""Tests for unified state store."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from agent_fleet.state import (
    LEGACY_ISSUE_FILENAME,
    LEGACY_PR_FILENAME,
    STATE_FILENAME,
    apply_issue_defaults,
    get_pr_state,
    load_state,
    save_state,
    set_pr_state,
    state_path,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_load_state_creates_empty_when_no_files(tmp_path: Path) -> None:
    path = state_path(tmp_path)
    assert load_state(path) == {}


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    path = state_path(tmp_path)
    save_state(path, {"foo": "bar", "pr:42": {"merged": True}})
    assert load_state(path) == {"foo": "bar", "pr:42": {"merged": True}}


def test_migration_merges_legacy_files(tmp_path: Path) -> None:
    issue_legacy = tmp_path / LEGACY_ISSUE_FILENAME
    pr_legacy = tmp_path / LEGACY_PR_FILENAME
    issue_legacy.write_text(
        json.dumps({"since": "2026-01-01T00:00:00Z", "seen": [1, 2]}),
        encoding="utf-8",
    )
    pr_legacy.write_text(
        json.dumps({"pr:99": {"merged": True}, "last_merge_ts": 1234.5}),
        encoding="utf-8",
    )

    state = load_state(state_path(tmp_path))

    assert state["since"] == "2026-01-01T00:00:00Z"
    assert state["seen"] == [1, 2]
    assert state["pr:99"] == {"merged": True}
    assert state["last_merge_ts"] == 1234.5
    assert (tmp_path / STATE_FILENAME).exists()
    assert not issue_legacy.exists()
    assert not pr_legacy.exists()
    assert (tmp_path / f"{LEGACY_ISSUE_FILENAME}.bak").exists()
    assert (tmp_path / f"{LEGACY_PR_FILENAME}.bak").exists()


def test_migration_skipped_when_unified_file_exists(tmp_path: Path) -> None:
    path = state_path(tmp_path)
    save_state(path, {"primary": True})
    (tmp_path / LEGACY_ISSUE_FILENAME).write_text(json.dumps({"legacy": True}), encoding="utf-8")

    state = load_state(path)
    assert state == {"primary": True}
    assert (tmp_path / LEGACY_ISSUE_FILENAME).exists()


def test_apply_issue_defaults() -> None:
    state: dict = {}
    apply_issue_defaults(state)
    assert state["seen"] == []
    assert state["in_flight"] == {}
    assert "since" in state


def test_apply_issue_defaults_respects_override() -> None:
    state: dict = {"since": "old"}
    apply_issue_defaults(state, since_override="new")
    assert state["since"] == "new"


def test_pr_state_helpers() -> None:
    state: dict = {}
    set_pr_state(state, 42, {"merged": True})
    assert get_pr_state(state, 42) == {"merged": True}
    assert get_pr_state(state, 99) == {}
