"""Tests for the one-agent PR ownership loop.

Two failures this guards against:

1. A round that starts from scratch. The point of the owner is memory: without
   the notes round-trip, every round re-derives what the last one already
   argued, and a disputed claim gets re-litigated forever.
2. A round whose bookkeeping lies. The notes are the only record of what was
   fixed and what was pushed, so a round that reports "fixed 2" without the
   notes recording it — or that records a fix it never made — is worse than no
   owner at all.

Everything here runs without network or engine: the backend is a capturing
fake, and git/gh are either scratch repos or not reached at all.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path  # noqa: TC003 - used as a concrete runtime path type

import pytest

from agent_fleet.contracts.gate import Finding
from agent_fleet.noop_session import NoopLLMResult
from agent_fleet.pr_owner import (
    ANSWER_TAG,
    NOTES_HISTORY_CHARS,
    build_fix_prompt,
    load_findings,
    own_round,
    parse_answer,
    pr_head,
    pr_notes_path,
    read_notes,
    read_task_spec,
    render_round,
    run_own,
    write_notes,
)


class _CapturingBackend:
    """Fake engine: records the prompt, returns a canned trailer."""

    def __init__(self, answer: dict[str, object] | None = None, *, exit_code: int = 0) -> None:
        self.prompts: list[str] = []
        self.calls: list[Path | None] = []
        self._answer = answer if answer is not None else {"fixed": ["1"], "disputed": []}
        self._exit_code = exit_code

    def run(
        self,
        prompt: str,
        *,
        max_tokens: int,  # noqa: ARG002
        timeout_s: int,  # noqa: ARG002
        memory_limit: str = "4G",  # noqa: ARG002
        allowed_tools: list[str] | None = None,  # noqa: ARG002
        cwd: Path | None = None,
        model: str | None = None,  # noqa: ARG002
        mode: object | None = None,  # noqa: ARG002
    ) -> NoopLLMResult:
        self.prompts.append(prompt)
        self.calls.append(cwd)
        block = json.dumps(self._answer, indent=2)
        return NoopLLMResult(
            stdout=f"work done\n\n{ANSWER_TAG}\n```json\n{block}\n```\n",
            stderr="",
            exit_code=self._exit_code,
            duration_s=0.1,
            agent_id="owner",
        )


def _finding(finding_id: str = "f-1") -> Finding:
    return Finding(
        id=finding_id,
        file="agent.py",
        line=3,
        claim="VALUE is wrong",
        repro="call VALUE -> 2, want 1",
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A scratch repo directory. Not a git repo — gh is faked, and own_round
    is only handed a worktree it never has to check out."""
    path = tmp_path / "repo"
    path.mkdir()
    return path


# ---------------------------------------------------------------------------
# Prompt assembly — one prompt, every input in it
# ---------------------------------------------------------------------------


def _prompt(**overrides: object) -> str:
    kwargs: dict[str, object] = {
        "pr_number": 7,
        "head": "abc123",
        "branch": "fb/pr-owner",
        "worktree": "/wt",
        "findings": [_finding()],
        "failing": [],
        "prior_notes": "",
        "test_command": "uv run pytest -q",
    }
    kwargs.update(overrides)
    return build_fix_prompt(**kwargs)  # type: ignore[arg-type]


def test_prompt_carries_every_finding_field() -> None:
    prompt = _prompt()
    assert "[f-1] agent.py:3" in prompt
    assert "claim: VALUE is wrong" in prompt
    assert "repro: call VALUE -> 2, want 1" in prompt


def test_prompt_numbers_findings_so_the_answer_can_key_on_them() -> None:
    """A model echoing a bare id will misspell it; the number is short and stable."""
    prompt = _prompt(findings=[_finding("a"), _finding("b")])
    assert "1. [a]" in prompt
    assert "2. [b]" in prompt


def test_prompt_includes_failing_test_ids() -> None:
    assert "tests/x.py::test_a" in _prompt(failing=["tests/x.py::test_a"])


def test_prompt_includes_prior_notes() -> None:
    assert "the claim was disputed last round" in _prompt(
        prior_notes="the claim was disputed last round"
    )


def test_prompt_bounds_how_much_history_travels_back() -> None:
    """Notes grow every round; an unbounded splice eventually crowds out the
    findings the round has to act on. The bound is in characters."""
    marker = "QQMARK"
    prompt = _prompt(prior_notes=marker * 20_000)
    assert prompt.count(marker) == NOTES_HISTORY_CHARS // len(marker)


def test_prompt_says_nothing_to_do_when_there_are_no_findings() -> None:
    assert "nothing to fix" in _prompt(findings=[])


def test_prompt_asks_for_the_tagged_answer_block() -> None:
    prompt = _prompt()
    assert ANSWER_TAG in prompt
    # The instruction names the tag and shows a json example right after it.
    assert '"fixed"' in prompt and '"disputed"' in prompt
    assert "fenced json block" in prompt


def test_prompt_carries_the_hard_process_safety_rules() -> None:
    """The owner runs on the same shared machine as every other lane."""
    prompt = _prompt()
    assert "PROCESS SAFETY" in prompt
    assert "NO BLOCKING COMMANDS" in prompt


def test_prompt_forbids_the_commands_that_destroy_other_lanes_work() -> None:
    prompt = _prompt()
    assert "git reset --hard" in prompt


# ---------------------------------------------------------------------------
# The answer block
# ---------------------------------------------------------------------------


def test_parse_answer_reads_fixed_and_disputed() -> None:
    stdout = (
        f"done\n{ANSWER_TAG}\n```json\n"
        + json.dumps(
            {"fixed": ["1", "2"], "disputed": [{"id": "3", "why": "the repro does not reproduce"}]}
        )
        + "\n```\n"
    )
    answer = parse_answer(stdout)
    assert answer["fixed"] == ["1", "2"]
    assert answer["disputed"] == [{"id": "3", "why": "the repro does not reproduce"}]


def test_parse_answer_tolerates_a_missing_block() -> None:
    """A garbled trailer must not read as "fixed nothing" and loop forever."""
    assert parse_answer("I fixed the bug.") == {"fixed": [], "disputed": []}


def test_parse_answer_tolerates_malformed_json() -> None:
    assert parse_answer(f"{ANSWER_TAG}\n```json\nnot json\n```") == {"fixed": [], "disputed": []}


def test_parse_answer_takes_the_last_block_when_a_model_rambles() -> None:
    stdout = (
        f"{ANSWER_TAG}\n```json\n" + json.dumps({"fixed": ["1"]}) + "\n```\n"
        f"wait, also:\n{ANSWER_TAG}\n```json\n" + json.dumps({"fixed": ["1", "2"]}) + "\n```\n"
    )
    assert parse_answer(stdout)["fixed"] == ["1", "2"]


# ---------------------------------------------------------------------------
# Notes round-tripping — the memory that carries between rounds
# ---------------------------------------------------------------------------


def test_notes_path_is_per_pr(repo: Path) -> None:
    assert pr_notes_path(repo, 7) == repo / ".agent-fleet" / "pr" / "7" / "notes.md"
    assert pr_notes_path(repo, 8) != pr_notes_path(repo, 7)


def test_reading_notes_for_an_untouched_pr_is_empty(repo: Path) -> None:
    assert read_notes(repo, 7) == ""


def test_write_then_read_round_trips(repo: Path) -> None:
    write_notes(repo, 7, "first observation")
    assert read_notes(repo, 7).strip() == "first observation"


def test_write_notes_ends_with_a_newline(repo: Path) -> None:
    write_notes(repo, 7, "no trailing newline")
    assert read_notes(repo, 7) == "no trailing newline\n"


def test_render_round_records_what_was_fixed_and_what_was_disputed() -> None:
    block = render_round(
        head="aaa",
        findings=[_finding()],
        fixed=["1"],
        disputed=[{"id": "2", "why": "already correct"}],
        new_head="bbb",
        timestamp="2026-01-01T00:00:00+00:00",
    )
    assert "fixed: 1" in block
    assert "disputed: 2: already correct" in block
    assert "aaa" in block and "bbb" in block
    assert "tests: pass" in block


def test_render_round_records_an_empty_round_too() -> None:
    """A round that fixed nothing is a real answer and must still be written
    down, or the next round cannot tell it apart from a round that never ran."""
    block = render_round(head="aaa", findings=[], timestamp="t")
    assert "fixed: none" in block
    assert "findings in: 0" in block


# ---------------------------------------------------------------------------
# Findings loading
# ---------------------------------------------------------------------------


def test_load_findings_reads_the_gate_report_dialect(tmp_path: Path) -> None:
    path = tmp_path / "f.json"
    path.write_text(json.dumps({"findings": [_finding().to_dict()]}), encoding="utf-8")
    assert [f.id for f in load_findings(path)] == ["f-1"]


def test_load_findings_reads_a_bare_list(tmp_path: Path) -> None:
    path = tmp_path / "f.json"
    path.write_text(json.dumps([_finding().to_dict()]), encoding="utf-8")
    assert [f.id for f in load_findings(path)] == ["f-1"]


def test_load_findings_with_no_file_is_empty() -> None:
    assert load_findings(None) == []


def test_load_findings_rejects_a_scalar(tmp_path: Path) -> None:
    path = tmp_path / "f.json"
    path.write_text("42", encoding="utf-8")
    with pytest.raises(ValueError):
        load_findings(path)


# ---------------------------------------------------------------------------
# The round, with a fake engine and no network
# ---------------------------------------------------------------------------


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    wt.mkdir()
    _git(wt, "init", "-b", "fb/pr-owner")
    _git(wt, "config", "user.email", "t@example.com")
    _git(wt, "config", "user.name", "T")
    (wt / "README.md").write_text("x\n", encoding="utf-8")
    _git(wt, "add", "README.md")
    _git(wt, "commit", "-m", "init")
    return wt


def _fake_head(monkeypatch: pytest.MonkeyPatch, repo: Path, oid: str = "aaa111") -> None:
    monkeypatch.setattr(
        "agent_fleet.pr_owner.gh",
        lambda *a, **k: subprocess.CompletedProcess(  # noqa: ARG005
            args=a,
            returncode=0,
            stdout=json.dumps({"headRefName": "fb/pr-owner", "headRefOid": oid}),
            stderr="",
        ),
    )
    del repo


def test_round_runs_the_engine_exactly_once(
    repo: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: one engine call per round, not one per finding."""
    _fake_head(monkeypatch, repo)
    backend = _CapturingBackend()
    own_round(
        repo_path=repo,
        pr_number=7,
        findings=[_finding("a"), _finding("b")],
        backend=backend,
        worktree=worktree,
    )
    assert len(backend.prompts) == 1
    assert backend.calls == [worktree]


def test_round_prompt_contains_the_findings_it_was_given(
    repo: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_head(monkeypatch, repo)
    backend = _CapturingBackend()
    own_round(
        repo_path=repo, pr_number=7, findings=[_finding("a")], backend=backend, worktree=worktree
    )
    assert "[a]" in backend.prompts[0]


def test_round_seeds_the_notes_from_the_task_spec(
    repo: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The spec is the one thing no earlier round can recover on its own."""
    _fake_head(monkeypatch, repo)
    own_round(
        repo_path=repo,
        pr_number=7,
        findings=[],
        task_spec="ship the thing",
        backend=_CapturingBackend(),
        worktree=worktree,
    )
    assert "ship the thing" in read_notes(repo, 7)


def test_round_two_sees_round_one_s_notes(
    repo: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The context-carrying property: round 2's prompt contains round 1's outcome."""
    _fake_head(monkeypatch, repo)
    first = _CapturingBackend(answer={"fixed": ["1"], "disputed": [{"id": "2", "why": "nope"}]})
    own_round(
        repo_path=repo,
        pr_number=7,
        findings=[_finding("a"), _finding("b")],
        backend=first,
        worktree=worktree,
    )

    second = _CapturingBackend()
    own_round(
        repo_path=repo, pr_number=7, findings=[_finding("c")], backend=second, worktree=worktree
    )

    prompt = second.prompts[0]
    assert "disputed: 2: nope" in prompt
    assert "[c]" in prompt


def test_round_appends_rather_than_overwrites(
    repo: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_head(monkeypatch, repo)
    for _ in range(3):
        own_round(
            repo_path=repo,
            pr_number=7,
            findings=[_finding()],
            backend=_CapturingBackend(),
            worktree=worktree,
        )
    notes = read_notes(repo, 7)
    assert notes.count("### Round") == 3


def test_round_reports_what_the_engine_claimed_to_fix(
    repo: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_head(monkeypatch, repo)
    backend = _CapturingBackend(answer={"fixed": ["1"], "disputed": [{"id": "2", "why": "w"}]})
    result = own_round(
        repo_path=repo, pr_number=7, findings=[_finding()], backend=backend, worktree=worktree
    )
    assert result.fixed == ["1"]
    assert result.disputed == [{"id": "2", "why": "w"}]


def test_round_reports_no_push_when_the_head_did_not_move(
    repo: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_head(monkeypatch, repo, oid=_git(worktree, "rev-parse", "HEAD"))
    result = own_round(
        repo_path=repo,
        pr_number=7,
        findings=[_finding()],
        backend=_CapturingBackend(),
        worktree=worktree,
    )
    assert result.pushed is False


def test_round_pushes_when_the_head_moved(
    repo: Path,
    worktree: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real push to a local bare remote: the round must move the remote head
    to the sha the engine committed, not merely claim to have pushed."""
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        capture_output=True,
        check=True,
    )
    _git(worktree, "remote", "add", "origin", str(remote))
    _git(worktree, "push", "-u", "origin", "fb/pr-owner")

    _fake_head(monkeypatch, repo, oid="stale000")
    (worktree / "new.txt").write_text("fix\n", encoding="utf-8")
    _git(worktree, "add", "new.txt")
    _git(worktree, "commit", "-m", "fix")
    expected = _git(worktree, "rev-parse", "HEAD")

    result = own_round(
        repo_path=repo,
        pr_number=7,
        findings=[_finding()],
        backend=_CapturingBackend(),
        worktree=worktree,
    )

    assert result.pushed is True
    assert result.new_head == expected
    remote_head = subprocess.run(
        ["git", "rev-parse", "fb/pr-owner"],
        cwd=remote,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert remote_head == expected


def test_round_records_a_failed_engine_without_claiming_a_fix(
    repo: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_head(monkeypatch, repo)
    result = own_round(
        repo_path=repo,
        pr_number=7,
        findings=[_finding()],
        backend=_CapturingBackend(exit_code=1),
        worktree=worktree,
    )
    assert result.pushed is False
    assert result.fixed == []
    assert "engine failed" in result.detail


def test_two_prs_keep_separate_notes(
    repo: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One PR's rounds must never leak into another's context."""
    _fake_head(monkeypatch, repo)
    own_round(
        repo_path=repo,
        pr_number=7,
        findings=[_finding()],
        task_spec="ship seven",
        backend=_CapturingBackend(),
        worktree=worktree,
    )
    own_round(
        repo_path=repo,
        pr_number=9,
        findings=[_finding()],
        task_spec="ship nine",
        backend=_CapturingBackend(),
        worktree=worktree,
    )

    seven, nine = read_notes(repo, 7), read_notes(repo, 9)
    assert "ship seven" in seven and "ship nine" not in seven
    assert "ship nine" in nine and "ship seven" not in nine


def test_pr_head_reads_both_fields_from_one_call(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def _gh(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:  # noqa: ARG001
        calls.append(args)
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps({"headRefName": "fb/x", "headRefOid": "deadbeef"}),
            stderr="",
        )

    monkeypatch.setattr("agent_fleet.pr_owner.gh", _gh)
    branch, oid = pr_head(7, repo)
    assert (branch, oid) == ("fb/x", "deadbeef")
    # One call, so a push landing between two calls cannot desync branch and sha.
    assert len(calls) == 1


def test_pr_head_raises_a_readable_error(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agent_fleet.pr_owner.gh",
        lambda *a, **k: subprocess.CompletedProcess(  # noqa: ARG005
            args=a,
            returncode=1,
            stdout="",
            stderr="no such PR",
        ),
    )
    with pytest.raises(RuntimeError, match="no such PR"):
        pr_head(999, repo)


# ---------------------------------------------------------------------------
# run_own — the raw-input entry the CLI calls
# ---------------------------------------------------------------------------


def test_run_own_reports_a_missing_repo_without_raising(tmp_path: Path) -> None:
    """The CLI prints one message and exits 1; it does not catch."""
    result = run_own(repo_path=tmp_path / "nope", pr_number=1)
    assert "repo path" in result["error"]


def test_run_own_reports_unreadable_findings_instead_of_raising(tmp_path: Path) -> None:
    bad = tmp_path / "f.json"
    bad.write_text("{not json", encoding="utf-8")
    result = run_own(repo_path=tmp_path, pr_number=1, findings_path=str(bad))
    assert result["error"]


def test_read_task_spec_with_no_file_is_empty() -> None:
    assert read_task_spec(None) == ""
