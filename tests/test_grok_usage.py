"""Grok token-usage accounting — unit tests (mocked subprocess; no live network).

Covers reading Grok CLI's per-session ``updates.jsonl`` (cumulative usage),
diffing consecutive reads into deltas, and the graceful-failure paths
(missing session dir, missing/corrupt updates.jsonl, no usage lines yet).
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from agent_fleet import grok_backend
from agent_fleet.grok_backend import GrokBackend, GrokSession, _encode_cwd


@pytest.fixture(autouse=True)
def _reset_usage_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the module-level delta-tracking dict across tests."""
    monkeypatch.setattr(grok_backend, "_last_session_usage", {})


def _write_updates_jsonl(
    sessions_root: Path,
    work_dir: str,
    session_id: str,
    usage_objects: list[dict[str, Any]],
) -> Path:
    session_dir = sessions_root / _encode_cwd(work_dir) / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / "updates.jsonl"
    lines = [json.dumps({"usage": obj}) for obj in usage_objects]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --- _encode_cwd ------------------------------------------------------------


def test_encode_cwd_matches_grok_cli_convention() -> None:
    assert (
        _encode_cwd("/tmp/agent-fleet-worktrees/task-0-5970acce")
        == "%2Ftmp%2Fagent-fleet-worktrees%2Ftask-0-5970acce"
    )


# --- GrokSession.send: normal read + cumulative delta -----------------------


def test_session_send_populates_usage_from_updates_jsonl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions_root = tmp_path / "sessions"
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    monkeypatch.setattr(grok_backend, "GROK_SESSIONS_ROOT", sessions_root)

    session_id = "11111111-1111-1111-1111-111111111111"
    _write_updates_jsonl(
        sessions_root,
        str(work_dir),
        session_id,
        [{"inputTokens": 100, "outputTokens": 50, "totalTokens": 150, "cachedReadTokens": 10}],
    )

    monkeypatch.setattr(grok_backend, "call_grok", lambda *a, **kw: "reply")  # noqa: ARG005
    session = GrokSession(
        grok_bin="/bin/grok", model="grok-4.5", cwd=work_dir, session_id=session_id
    )
    result = session.send("hi", max_tokens=10, timeout_s=30)

    assert result.exit_code == 0
    assert result.usage == {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 10,
        "total_tokens": 150,
    }


def test_session_send_emits_delta_across_two_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions_root = tmp_path / "sessions"
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    monkeypatch.setattr(grok_backend, "GROK_SESSIONS_ROOT", sessions_root)

    session_id = "22222222-2222-2222-2222-222222222222"
    path = _write_updates_jsonl(
        sessions_root,
        str(work_dir),
        session_id,
        [{"inputTokens": 100, "outputTokens": 50, "cachedReadTokens": 10}],
    )

    monkeypatch.setattr(grok_backend, "call_grok", lambda *a, **kw: "reply")  # noqa: ARG005
    session = GrokSession(
        grok_bin="/bin/grok", model="grok-4.5", cwd=work_dir, session_id=session_id
    )

    first = session.send("one", max_tokens=10, timeout_s=30)
    assert first.usage == {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 10,
        "total_tokens": 150,
    }

    # Simulate the Grok CLI appending a new cumulative usage line for the
    # second turn (updates.jsonl is cumulative, not per-call).
    with path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps({"usage": {"inputTokens": 260, "outputTokens": 90, "cachedReadTokens": 15}})
            + "\n"
        )

    second = session.send("two", max_tokens=10, timeout_s=30)
    # Delta only — not the raw cumulative totals — so rollups don't double-count.
    assert second.usage == {
        "input_tokens": 160,
        "output_tokens": 40,
        "cache_read_tokens": 5,
        "total_tokens": 200,
    }


def test_session_send_no_usage_line_yet_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions_root = tmp_path / "sessions"
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    monkeypatch.setattr(grok_backend, "GROK_SESSIONS_ROOT", sessions_root)

    session_id = "33333333-3333-3333-3333-333333333333"
    session_dir = sessions_root / _encode_cwd(str(work_dir)) / session_id
    session_dir.mkdir(parents=True)
    (session_dir / "updates.jsonl").write_text(
        json.dumps({"type": "toolCall", "id": "abc"}) + "\n", encoding="utf-8"
    )

    monkeypatch.setattr(grok_backend, "call_grok", lambda *a, **kw: "reply")  # noqa: ARG005
    session = GrokSession(
        grok_bin="/bin/grok", model="grok-4.5", cwd=work_dir, session_id=session_id
    )
    result = session.send("hi", max_tokens=10, timeout_s=30)
    assert result.exit_code == 0
    assert result.usage is None


# --- Graceful failure paths --------------------------------------------------


def test_missing_session_dir_yields_no_usage_no_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions_root = tmp_path / "sessions"  # never created
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    monkeypatch.setattr(grok_backend, "GROK_SESSIONS_ROOT", sessions_root)

    monkeypatch.setattr(grok_backend, "call_grok", lambda *a, **kw: "reply")  # noqa: ARG005
    session = GrokSession(
        grok_bin="/bin/grok",
        model="grok-4.5",
        cwd=work_dir,
        session_id="44444444-4444-4444-4444-444444444444",
    )
    result = session.send("hi", max_tokens=10, timeout_s=30)
    assert result.exit_code == 0
    assert result.usage is None


def test_corrupt_updates_jsonl_is_skipped_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions_root = tmp_path / "sessions"
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    monkeypatch.setattr(grok_backend, "GROK_SESSIONS_ROOT", sessions_root)

    session_id = "55555555-5555-5555-5555-555555555555"
    session_dir = sessions_root / _encode_cwd(str(work_dir)) / session_id
    session_dir.mkdir(parents=True)
    # Entirely corrupt file — no valid JSON lines at all.
    (session_dir / "updates.jsonl").write_text("{not valid json\n{also broken\n", encoding="utf-8")

    monkeypatch.setattr(grok_backend, "call_grok", lambda *a, **kw: "reply")  # noqa: ARG005
    session = GrokSession(
        grok_bin="/bin/grok", model="grok-4.5", cwd=work_dir, session_id=session_id
    )
    result = session.send("hi", max_tokens=10, timeout_s=30)
    assert result.exit_code == 0
    assert result.usage is None


def test_corrupt_line_skipped_valid_line_still_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions_root = tmp_path / "sessions"
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    monkeypatch.setattr(grok_backend, "GROK_SESSIONS_ROOT", sessions_root)

    session_id = "66666666-6666-6666-6666-666666666666"
    session_dir = sessions_root / _encode_cwd(str(work_dir)) / session_id
    session_dir.mkdir(parents=True)
    (session_dir / "updates.jsonl").write_text(
        "{corrupt\n" + json.dumps({"usage": {"inputTokens": 5, "outputTokens": 3}}) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(grok_backend, "call_grok", lambda *a, **kw: "reply")  # noqa: ARG005
    session = GrokSession(
        grok_bin="/bin/grok", model="grok-4.5", cwd=work_dir, session_id=session_id
    )
    result = session.send("hi", max_tokens=10, timeout_s=30)
    assert result.exit_code == 0
    assert result.usage == {
        "input_tokens": 5,
        "output_tokens": 3,
        "total_tokens": 8,
    }


# --- GrokBackend.run: one-shot session resolution via summary.json ---------


def test_run_resolves_session_by_cwd_when_no_session_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(grok_backend, "check_grok_auth", lambda: (True, "ok", ""))
    sessions_root = tmp_path / "sessions"
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    monkeypatch.setattr(grok_backend, "GROK_SESSIONS_ROOT", sessions_root)

    session_id = "77777777-7777-7777-7777-777777777777"

    # The real Grok CLI creates the session dir (summary.json + updates.jsonl)
    # *during* the call, i.e. strictly after call_started_at is captured.
    # Simulate that ordering by writing them from inside the fake call_grok.
    def _fake_call_grok(*_a: object, **_kw: object) -> str:
        session_dir = sessions_root / _encode_cwd(str(work_dir)) / session_id
        session_dir.mkdir(parents=True)
        (session_dir / "summary.json").write_text(
            json.dumps(
                {"info": {"id": session_id, "cwd": str(work_dir), "createdAt": time.time()}}
            ),
            encoding="utf-8",
        )
        (session_dir / "updates.jsonl").write_text(
            json.dumps({"usage": {"inputTokens": 42, "outputTokens": 8}}) + "\n",
            encoding="utf-8",
        )
        return "reply"

    monkeypatch.setattr(grok_backend, "call_grok", _fake_call_grok)
    backend = GrokBackend(grok_bin="/bin/grok", model="grok-4.5")
    result = backend.run("do it", max_tokens=10, timeout_s=30, cwd=work_dir)

    assert result.exit_code == 0
    assert result.usage == {
        "input_tokens": 42,
        "output_tokens": 8,
        "total_tokens": 50,
    }


def test_run_ambiguous_session_match_skips_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(grok_backend, "check_grok_auth", lambda: (True, "ok", ""))
    sessions_root = tmp_path / "sessions"
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    monkeypatch.setattr(grok_backend, "GROK_SESSIONS_ROOT", sessions_root)

    def _fake_call_grok(*_a: object, **_kw: object) -> str:
        now = time.time()
        for sid in (
            "aaaa1111-0000-0000-0000-000000000001",
            "bbbb2222-0000-0000-0000-000000000002",
        ):
            session_dir = sessions_root / _encode_cwd(str(work_dir)) / sid
            session_dir.mkdir(parents=True)
            (session_dir / "summary.json").write_text(
                json.dumps({"info": {"id": sid, "cwd": str(work_dir), "createdAt": now}}),
                encoding="utf-8",
            )
            (session_dir / "updates.jsonl").write_text(
                json.dumps({"usage": {"inputTokens": 1, "outputTokens": 1}}) + "\n",
                encoding="utf-8",
            )
        return "reply"

    monkeypatch.setattr(grok_backend, "call_grok", _fake_call_grok)
    backend = GrokBackend(grok_bin="/bin/grok", model="grok-4.5")
    result = backend.run("do it", max_tokens=10, timeout_s=30, cwd=work_dir)

    assert result.exit_code == 0
    assert result.usage is None
