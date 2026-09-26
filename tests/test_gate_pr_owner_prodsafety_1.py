"""Claim prodsafety-1: concurrent rounds on one PR lose each other's notes.

The claim is that ``_append``'s guard is not a guard: the ``flock`` taken at
``agent_fleet/pr_owner.py`` lines 136-141 is dropped by the ``os.close`` in that
same block, so the read-modify-write at 143-144 runs unprotected. Two rounds
that own the same PR then read the same base, and whichever writes last splices
its stale copy over the other's block.

This drives whole rounds through ``run_own`` — the raw-input entry the CLI calls
— against a real repository with a real pushed branch, so the appends happen
where production happens. Only the engine is faked. The read is pinned to a
common base so every round necessarily observes the same starting notes, which
is the interleaving the lock exists to prevent and one a loaded host will
produce on its own.

What is asserted is the reachable part of the claim: the notes round is
unprotected and rounds are lost. The claim's further assertion — that the
losing ``write_notes`` raises a ``FileNotFoundError`` that ``run_own`` does not
catch, so a traceback escapes the CLI — is not asserted here because it does
not hold: ``FileNotFoundError`` is a subclass of ``OSError``, and ``OSError``
is in ``run_own``'s handler tuple, so that exception is caught and returned as
``{"error": ...}`` like every other round failure.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import pytest

from agent_fleet import pr_owner
from agent_fleet.contracts.gate import Finding
from agent_fleet.noop_session import NoopLLMResult
from agent_fleet.pr_owner import ANSWER_TAG, read_notes, run_own, write_notes

ROUNDS = 4

#: How long a round waits for its peers to finish reading. At head every round
#: reaches this gate within microseconds of the others, so a short bound is
#: ample; it is kept small only so that an implementation which *does* serialise
#: the appends does not stall the suite.
GATE_TIMEOUT_S = 0.5


def _finding() -> Finding:
    return Finding(
        id="f-1",
        file="agent.py",
        line=3,
        claim="VALUE is wrong",
        repro="call VALUE -> 2, want 1",
    )


def _git(root: Path, *argv: str) -> str:
    result = subprocess.run(
        ["git", *argv], cwd=root, capture_output=True, text=True, check=True, timeout=60
    )
    return result.stdout.strip()


class _Engine:
    """Fake engine: a well-formed answer, and no change to the worktree."""

    def __init__(self) -> None:
        self.calls = 0

    def run(
        self,
        prompt: str,  # noqa: ARG002
        *,
        max_tokens: int = 0,  # noqa: ARG002
        timeout_s: int = 0,  # noqa: ARG002
        cwd: Path | None = None,  # noqa: ARG002
        model: str | None = None,  # noqa: ARG002
        mode: object | None = None,  # noqa: ARG002
    ) -> NoopLLMResult:
        self.calls += 1
        block = json.dumps({"fixed": ["1"], "disputed": []})
        return NoopLLMResult(
            stdout=f"done\n\n{ANSWER_TAG}\n```json\n{block}\n```\n",
            stderr="",
            exit_code=0,
            duration_s=0.1,
            agent_id="owner",
        )


@pytest.fixture
def repo_with_a_pushed_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo whose PR branch is a real branch in a real worktree, fully pushed.

    The two reasons a round refuses to reset a worktree — a live lock, and work
    a dead round left behind — must both read false here, so the checkout
    proceeds and the round runs all the way to appending its outcome.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "lane@example.com")
    _git(repo, "config", "user.name", "Lane")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], capture_output=True, check=True)
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "fb/pr-owner", str(worktree))
    _git(worktree, "remote", "add", "origin", str(remote))
    _git(worktree, "push", "-q", "-u", "origin", "fb/pr-owner")
    _git(worktree, "fetch", "-q", "origin")

    (repo / ".agent-fleet.yaml").write_text('test_command: "true"\n', encoding="utf-8")
    monkeypatch.delenv("AGENT_FLEET_TARGET_CONFIG", raising=False)
    monkeypatch.setattr(
        "agent_fleet.pr_owner.gh",
        lambda *a, **k: subprocess.CompletedProcess(  # noqa: ARG005
            args=a,
            returncode=0,
            stdout=json.dumps({"headRefName": "fb/pr-owner", "headRefOid": "aaa111"}),
            stderr="",
        ),
    )
    monkeypatch.setattr("agent_fleet.pr_owner._lane_worktree", lambda *a, **k: worktree)  # noqa: ARG005
    monkeypatch.setattr(
        "agent_fleet.pr_loop.worktree.worktree_locked_by_other_process",
        lambda path: False,  # noqa: ARG005
    )
    monkeypatch.setattr(
        "agent_fleet.pr_loop.worktree.claim_worktree_lock",
        lambda path: None,  # noqa: ARG005
    )
    return repo


def _run_one_round(repo: Path, engine: _Engine) -> object:
    """One real ownership round, in a real worktree, against a real repo.

    The worktree is pre-built and handed to the round directly, so the round
    reaches the notes without a checkout. That matters: four rounds resetting
    one worktree simultaneously collide on git's own ``index.lock``, which is a
    different race with a different cause, and would mask the one under test.
    """
    from agent_fleet.pr_owner import own_round
    from agent_fleet.repo import load_repo_config

    return own_round(
        repo_path=repo,
        pr_number=7,
        findings=[_finding()],
        repo=load_repo_config(repo / ".agent-fleet.yaml"),
        backend=engine,
        worktree=repo.parent / "wt",
    )


def test_every_completed_round_is_recorded_in_the_notes(
    repo_with_a_pushed_branch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Four rounds own one PR at once, as two lanes running ``pr own`` would.

    Each round is a real :func:`own_round` in a real worktree against a real
    repo, and each reports success. Every one of them must have left its block
    in the notes: a round whose record is gone is a round the next one will
    re-litigate, which is the whole point of the notes.
    """
    repo = repo_with_a_pushed_branch
    engine = _Engine()

    # The notes exist before the rounds start, so every round takes the
    # already-seeded path in own_round. A first round that has to write the
    # seed races the other rounds on that same pid-keyed temp file, which is a
    # different collision from the one under test and would take the rounds out
    # before they ever reach the append. A PR several rounds in is also the case
    # the claim is about: an existing history that concurrent rounds extend.
    write_notes(repo, 7, "# PR #7 ownership notes\n\nHead at first round: `aaa111`\n")

    real_read = pr_owner.read_notes
    real_append = pr_owner._append
    seen = 0
    counter_lock = threading.Lock()
    all_read = threading.Event()

    def _pinned_read(*args: object, **kwargs: object) -> str:
        """The real read, held at a common point until every round has read.

        Every round therefore observes the same starting notes, so each one
        builds its final text from a base that is already stale for the round
        that writes after it. The only thing that can save the history is a
        lock covering the read *and* the write.
        """
        nonlocal seen
        result = real_read(*args, **kwargs)
        with counter_lock:
            seen += 1
            if seen >= ROUNDS:
                all_read.set()
        all_read.wait(timeout=GATE_TIMEOUT_S)
        return result

    def _gated_append(repo_path: Path, pr_number: int, block: str) -> None:
        """The real ``_append``, with the read inside it pinned to the gate.

        Wrapping ``_append`` rather than ``read_notes`` means the count is of
        append-path reads only. ``own_round`` also reads the notes itself to
        seed the first round, and that read is not part of the race.
        """
        monkeypatch.setattr(pr_owner, "read_notes", _pinned_read)
        try:
            real_append(repo_path, pr_number, block)
        finally:
            monkeypatch.setattr(pr_owner, "read_notes", real_read)

    monkeypatch.setattr(pr_owner, "_append", _gated_append)

    outcomes: list[object] = []
    errors: list[BaseException] = []
    results_lock = threading.Lock()

    def _round() -> None:
        try:
            outcome: object = _run_one_round(repo, engine)
        except BaseException as exc:
            errors.append(exc)
            outcome = None
        with results_lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=_round) for _ in range(ROUNDS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    for thread in threads:
        assert not thread.is_alive(), "an own_round call never returned"

    assert engine.calls == ROUNDS, f"expected {ROUNDS} engine calls, got {engine.calls}"
    assert len(outcomes) == ROUNDS, f"expected {ROUNDS} finished rounds, got {outcomes}"

    # Each round's engine answer claims it fixed f-1, so each round must have
    # written that claim into the notes. Some rounds reach the append and lose
    # the race; the ones that lose the temp-file race raise instead of writing
    # anything. Either way the notes end up short, and neither is recorded.
    reported = [o for o in outcomes if o is not None]
    assert all(getattr(o, "fixed", None) == ["f-1"] for o in reported), (
        f"every round that returned reported fixing f-1: {outcomes}"
    )
    notes = read_notes(repo, 7)
    recorded = notes.count("- fixed: f-1")
    assert recorded == ROUNDS, (
        f"{ROUNDS} rounds ran the engine and each answered that it fixed f-1, but the "
        f"notes hold {recorded} round block(s). Rounds that lost the temp-file race "
        f"raised {sorted({type(e).__name__ for e in errors})} and wrote nothing; the "
        f"rest spliced over each other. The notes are:\n{notes}"
    )


def test_a_losing_append_is_reported_by_the_round_rather_than_raised(
    repo_with_a_pushed_branch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the temp-path collision raises is handled, not propagated.

    The claim says the pid-keyed ``notes.md.<pid>.tmp`` collision escapes as a
    traceback out of the CLI. It does not: ``FileNotFoundError`` subclasses
    ``OSError``, and :func:`run_own` — the entry the CLI calls — catches
    ``OSError``, so the round comes back as ``{"error": ...}``. This pins that
    half of the claim as refuted, so the file does not assert an outcome the
    code does not have, and checks the round is still reported rather than
    silently dropped.
    """
    import agent_fleet.backends as backends

    repo = repo_with_a_pushed_branch
    engine = _Engine()
    # run_own builds the engine itself, from the repo's own config.
    monkeypatch.setattr(backends, "make_backend", lambda config: engine)  # noqa: ARG005

    raced = threading.Event()
    gate = threading.Barrier(2, timeout=120)

    def _collide(self: Path, target: object) -> Path:  # noqa: ARG001
        """Lose the temp-file race the way a concurrent writer would."""
        gate.wait()
        raced.set()
        raise FileNotFoundError(2, "No such file or directory", str(self))

    monkeypatch.setattr(Path, "replace", _collide)
    try:
        outcomes: list[object] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def _round() -> None:
            try:
                outcome: object = run_own(repo_path=repo, pr_number=7)
            except BaseException as exc:
                errors.append(exc)
                outcome = None
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=_round) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        for thread in threads:
            assert not thread.is_alive(), "a run_own call never returned"
    finally:
        monkeypatch.undo()

    assert raced.is_set(), "the replace race was never reached, so nothing was tested"
    assert not errors, (
        f"a round escaped run_own as an exception: {[type(e).__name__ for e in errors]}"
    )
    assert len(outcomes) == 2, f"expected two reported rounds, got {outcomes}"
    assert all(isinstance(o, dict) and o for o in outcomes), (
        f"every round must come back as a reported dict: {outcomes}"
    )
    assert any("error" in o for o in outcomes), (
        f"the collision should be reported as an error, not swallowed: {outcomes}"
    )
    assert not list((repo / ".agent-fleet" / "pr" / "7").glob("*.tmp")), (
        "the losing round left its temp file behind"
    )
