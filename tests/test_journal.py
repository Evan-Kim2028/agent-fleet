"""Tests for agent_fleet.orchestration.journal."""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING

from agent_fleet.orchestration.journal import (
    AgentRecord,
    RunEvent,
    RunEventKind,
    RunJournal,
    fold,
    fold_journal,
    load_journal,
    query_by_run,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    run_id: str = "run-1",
    seq: int = 0,
    kind: RunEventKind = RunEventKind.log,
    ts: float = 1.0,
    agent_id: str | None = None,
    task_index: int | None = None,
    payload: dict | None = None,
) -> RunEvent:
    return RunEvent(
        run_id=run_id,
        seq=seq,
        kind=kind,
        ts=ts,
        agent_id=agent_id,
        task_index=task_index,
        payload=payload or {},
    )


# ---------------------------------------------------------------------------
# RunEvent JSONL round-trip
# ---------------------------------------------------------------------------


def test_run_event_to_dict_and_from_dict_lossless() -> None:
    evt = RunEvent(
        run_id="run-42",
        seq=7,
        kind=RunEventKind.agent_completed,
        ts=1_700_000_000.123,
        agent_id="agent-abc",
        task_index=3,
        payload={"status": "completed", "summary": "great work", "observed_total_tokens": 512},
    )
    d = evt.to_dict()
    reconstructed = RunEvent.from_dict(d)
    assert reconstructed == evt


def test_run_event_from_dict_kind_is_enum() -> None:
    d = {
        "run_id": "r1",
        "seq": 0,
        "kind": "agent_failed",
        "ts": 1.0,
        "agent_id": None,
        "task_index": None,
        "payload": {},
    }
    evt = RunEvent.from_dict(d)
    assert evt.kind is RunEventKind.agent_failed
    assert isinstance(evt.kind, RunEventKind)


def test_run_event_from_dict_none_optionals() -> None:
    d = {
        "run_id": "r1",
        "seq": 0,
        "kind": "log",
        "ts": 1.5,
        "agent_id": None,
        "task_index": None,
        "payload": {"message": "hello"},
    }
    evt = RunEvent.from_dict(d)
    assert evt.agent_id is None
    assert evt.task_index is None


def test_load_journal_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    events = [
        _make_event("r1", 0, RunEventKind.run_started),
        _make_event("r1", 1, RunEventKind.agent_started, agent_id="a1", task_index=0,
                    payload={"persona": "coder", "goal": "do stuff"}),
        _make_event("r1", 2, RunEventKind.agent_completed, agent_id="a1", task_index=0,
                    payload={"status": "completed", "summary": "done",
                             "observed_total_tokens": 100}),
        _make_event("r1", 3, RunEventKind.run_completed, payload={"status": "completed"}),
    ]
    with p.open("w") as fh:
        for e in events:
            fh.write(json.dumps(e.to_dict()) + "\n")
    loaded = load_journal(p)
    assert loaded == events


def test_load_journal_tolerates_blank_lines(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    evt = _make_event("r1", 0, RunEventKind.log, payload={"message": "hi"})
    with p.open("w") as fh:
        fh.write("\n")
        fh.write(json.dumps(evt.to_dict()) + "\n")
        fh.write("   \n")
    loaded = load_journal(p)
    assert loaded == [evt]


# ---------------------------------------------------------------------------
# fold_journal / fold alias
# ---------------------------------------------------------------------------


def test_fold_alias_is_fold_journal() -> None:
    assert fold is fold_journal


def test_fold_empty_events() -> None:
    state = fold_journal([])
    assert state.run_id == ""
    assert state.status == "unknown"
    assert state.agents == ()
    assert state.phases == ()
    assert state.log == ()


def test_fold_reconstructs_run_state() -> None:
    events = [
        _make_event("r1", 0, RunEventKind.run_started, ts=100.0),
        _make_event("r1", 1, RunEventKind.phase, payload={"title": "dispatch"}),
        _make_event("r1", 2, RunEventKind.agent_started, agent_id="a0", task_index=0,
                    payload={"persona": "coder", "goal": "implement X"}),
        _make_event("r1", 3, RunEventKind.agent_completed, agent_id="a0", task_index=0,
                    payload={"status": "completed", "summary": "done X",
                             "observed_total_tokens": 200}),
        _make_event("r1", 4, RunEventKind.log, payload={"message": "all good"}),
        _make_event("r1", 5, RunEventKind.run_completed, ts=200.0,
                    payload={"status": "completed"}),
    ]
    state = fold_journal(events)

    assert state.run_id == "r1"
    assert state.status == "completed"
    assert state.started_at == 100.0
    assert state.completed_at == 200.0
    assert state.phases == ("dispatch",)
    assert state.log == ("all good",)

    assert len(state.agents) == 1
    agent = state.agents[0]
    assert agent.task_index == 0
    assert agent.agent_id == "a0"
    assert agent.persona == "coder"
    assert agent.goal == "implement X"
    assert agent.status == "completed"
    assert agent.summary == "done X"
    assert agent.observed_total_tokens == 200
    assert agent.started is True
    assert agent.done is True
    assert agent.error is None
    assert agent.ok is True


def test_fold_agent_failed() -> None:
    events = [
        _make_event("r1", 0, RunEventKind.run_started),
        _make_event("r1", 1, RunEventKind.agent_started, agent_id="a0", task_index=0,
                    payload={"persona": "coder", "goal": "do thing"}),
        _make_event("r1", 2, RunEventKind.agent_failed, agent_id="a0", task_index=0,
                    payload={"status": "failed", "error": "timeout"}),
    ]
    state = fold_journal(events)
    agent = state.agent_by_index(0)
    assert agent is not None
    assert agent.ok is False
    assert agent.status == "failed"
    assert agent.error == "timeout"
    assert agent.done is True


def test_fold_completed_task_indices() -> None:
    events = [
        _make_event("r1", 0, RunEventKind.run_started),
        _make_event("r1", 1, RunEventKind.agent_completed, task_index=0,
                    payload={"status": "completed", "summary": "ok"}),
        _make_event("r1", 2, RunEventKind.agent_completed, task_index=1,
                    payload={"status": "merged", "summary": "merged"}),
        _make_event("r1", 3, RunEventKind.agent_failed, task_index=2,
                    payload={"status": "failed", "error": "boom"}),
    ]
    state = fold_journal(events)
    assert state.completed_task_indices == frozenset({0, 1})


# ---------------------------------------------------------------------------
# Idempotent fold (duplicate events)
# ---------------------------------------------------------------------------


def test_fold_idempotent_under_duplicate_events() -> None:
    events = [
        _make_event("r1", 0, RunEventKind.run_started),
        _make_event("r1", 1, RunEventKind.agent_started, agent_id="a0", task_index=0,
                    payload={"persona": "coder", "goal": "g"}),
        _make_event("r1", 2, RunEventKind.agent_completed, agent_id="a0", task_index=0,
                    payload={"status": "completed", "summary": "ok", "observed_total_tokens": 50}),
        _make_event("r1", 3, RunEventKind.run_completed, payload={"status": "completed"}),
    ]
    state_once = fold_journal(events)
    state_duped = fold_journal(events + events)  # full duplicate
    # RunState.agents and status should be identical; log lines may double.
    assert state_once.status == state_duped.status
    assert state_once.completed_task_indices == state_duped.completed_task_indices
    assert state_once.agents[0].status == state_duped.agents[0].status
    assert state_once.agents[0].summary == state_duped.agents[0].summary


def test_fold_stale_duplicate_does_not_undo_completed() -> None:
    """A replayed agent_completed (lower seq) must not clobber a later completed."""
    early_complete = _make_event("r1", 2, RunEventKind.agent_completed, task_index=0,
                                 payload={"status": "completed", "summary": "first"})
    # A stale replay arrives with seq=1 (lower than seq=2).
    stale_replay = _make_event("r1", 1, RunEventKind.agent_completed, task_index=0,
                               payload={"status": "failed", "summary": "stale"})
    events = [
        _make_event("r1", 0, RunEventKind.run_started),
        stale_replay,
        early_complete,
    ]
    state = fold_journal(events)
    # The higher-seq event wins.
    agent = state.agent_by_index(0)
    assert agent is not None
    assert agent.status == "completed"
    assert agent.summary == "first"


# ---------------------------------------------------------------------------
# Durability
# ---------------------------------------------------------------------------


def test_durability_raw_bytes_present_after_append(tmp_path: Path) -> None:
    p = tmp_path / "journal.jsonl"
    with RunJournal(p, "run-dur") as j:
        j.append(RunEventKind.log, message="durability-check")

    # Read raw bytes WITHOUT going through the journal object.
    raw = p.read_bytes().decode("utf-8")
    assert "durability-check" in raw
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["payload"]["message"] == "durability-check"


def test_durability_multiple_appends_each_fsynced(tmp_path: Path) -> None:
    p = tmp_path / "journal.jsonl"
    with RunJournal(p, "run-dur2") as j:
        j.append(RunEventKind.run_started)
        j.append(RunEventKind.log, message="msg-1")
        j.append(RunEventKind.log, message="msg-2")

    raw = p.read_text(encoding="utf-8")
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    assert len(lines) == 3


# ---------------------------------------------------------------------------
# Thread-safety
# ---------------------------------------------------------------------------


def test_thread_safe_concurrent_appends(tmp_path: Path) -> None:
    p = tmp_path / "concurrent.jsonl"
    n_threads = 10
    results: list[RunEvent] = []
    lock = threading.Lock()

    journal = RunJournal(p, "run-concurrent")
    barrier = threading.Barrier(n_threads)

    def worker(i: int) -> None:
        barrier.wait()  # all start at the same time
        evt = journal.append(RunEventKind.log, message=f"thread-{i}")
        with lock:
            results.append(evt)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    journal.close()

    # All N events must be present with unique seq values.
    assert len(results) == n_threads
    seqs = [e.seq for e in results]
    assert len(set(seqs)) == n_threads, f"duplicate seqs: {seqs}"
    assert sorted(seqs) == list(range(n_threads))

    # The file must also contain all N lines.
    loaded = load_journal(p)
    assert len(loaded) == n_threads
    file_seqs = sorted(e.seq for e in loaded)
    assert file_seqs == list(range(n_threads))


# ---------------------------------------------------------------------------
# query_by_run
# ---------------------------------------------------------------------------


def test_query_by_run_isolates_run_id_from_file(tmp_path: Path) -> None:
    p = tmp_path / "mixed.jsonl"
    events_r1 = [
        _make_event("r1", 0, RunEventKind.run_started, ts=1.0),
        _make_event("r1", 1, RunEventKind.agent_completed, task_index=0,
                    payload={"status": "completed", "summary": "ok"}),
        _make_event("r1", 2, RunEventKind.run_completed, payload={"status": "completed"}),
    ]
    events_r2 = [
        _make_event("r2", 0, RunEventKind.run_started, ts=2.0),
        _make_event("r2", 1, RunEventKind.agent_failed, task_index=0,
                    payload={"status": "failed", "error": "oops"}),
    ]
    with p.open("w") as fh:
        for e in events_r1 + events_r2:
            fh.write(json.dumps(e.to_dict()) + "\n")

    state_r1 = query_by_run(p, "r1")
    state_r2 = query_by_run(p, "r2")

    assert state_r1.run_id == "r1"
    assert state_r1.status == "completed"
    assert len(state_r1.agents) == 1
    assert state_r1.agents[0].ok is True

    assert state_r2.run_id == "r2"
    assert len(state_r2.agents) == 1
    assert state_r2.agents[0].ok is False


def test_query_by_run_accepts_preloaded_list() -> None:
    events = [
        _make_event("r1", 0, RunEventKind.run_started),
        _make_event("r2", 0, RunEventKind.run_started),
    ]
    state = query_by_run(events, "r1")
    assert state.run_id == "r1"
    # r2 events excluded
    assert state.status == "started"


# ---------------------------------------------------------------------------
# pending_task_indices
# ---------------------------------------------------------------------------


def test_pending_task_indices() -> None:
    events = [
        _make_event("r1", 0, RunEventKind.run_started),
        _make_event("r1", 1, RunEventKind.agent_completed, task_index=0,
                    payload={"status": "completed", "summary": "ok"}),
        _make_event("r1", 2, RunEventKind.agent_completed, task_index=2,
                    payload={"status": "merged", "summary": "merged"}),
        _make_event("r1", 3, RunEventKind.agent_failed, task_index=3,
                    payload={"status": "failed", "error": "err"}),
    ]
    state = fold_journal(events)
    expected = {0, 1, 2, 3}
    pending = state.pending_task_indices(expected)
    # 0 and 2 completed (ok), 3 failed (not ok), 1 never started
    assert pending == frozenset({1, 3})


def test_pending_task_indices_all_complete() -> None:
    events = [
        _make_event("r1", 0, RunEventKind.agent_completed, task_index=0,
                    payload={"status": "completed", "summary": "ok"}),
        _make_event("r1", 1, RunEventKind.agent_completed, task_index=1,
                    payload={"status": "completed", "summary": "ok2"}),
    ]
    state = fold_journal(events)
    assert state.pending_task_indices({0, 1}) == frozenset()


# ---------------------------------------------------------------------------
# RunJournal context manager and .events() / .state()
# ---------------------------------------------------------------------------


def test_run_journal_events_and_state(tmp_path: Path) -> None:
    p = tmp_path / "j.jsonl"
    with RunJournal(p, "run-j") as j:
        j.run_started()
        j.agent_started(0, agent_id="a0", persona="coder", goal="do it")
        j.agent_completed(0, agent_id="a0", status="completed", summary="done",
                          observed_total_tokens=77)
        j.run_completed(status="completed")

        evts = j.events()
        assert len(evts) == 4
        assert evts[0].kind == RunEventKind.run_started
        assert [e.seq for e in evts] == [0, 1, 2, 3]

        state = j.state()
        assert state.status == "completed"
        assert state.agents[0].persona == "coder"
        assert state.agents[0].observed_total_tokens == 77
        assert state.agents[0].ok is True


def test_run_journal_seq_monotonic(tmp_path: Path) -> None:
    p = tmp_path / "mono.jsonl"
    with RunJournal(p, "run-mono") as j:
        evts = [j.append(RunEventKind.log, message=f"m{i}") for i in range(5)]
    assert [e.seq for e in evts] == list(range(5))


def test_run_journal_close_is_idempotent(tmp_path: Path) -> None:
    p = tmp_path / "close.jsonl"
    j = RunJournal(p, "run-close")
    j.append(RunEventKind.log, message="x")
    j.close()
    j.close()  # should not raise


# ---------------------------------------------------------------------------
# AgentRecord.ok property
# ---------------------------------------------------------------------------


def test_agent_record_ok_statuses() -> None:
    def make(status: str) -> AgentRecord:
        return AgentRecord(
            task_index=0, agent_id=None, persona="p", goal="g",
            status=status, summary=None, observed_total_tokens=None,
            started=True, done=True, error=None,
        )

    assert make("completed").ok is True
    assert make("merged").ok is True
    assert make("review_changes_requested").ok is True
    assert make("failed").ok is False
    assert make("error").ok is False
    assert make("unknown").ok is False
