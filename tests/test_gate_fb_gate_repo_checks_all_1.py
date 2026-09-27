"""A REQUIRED CHECK must hold a test-pool slot, or the memory budget is fiction.

The gate documents that every memory-hungry subprocess it launches holds a
test-pool slot for its duration, and the test pool is sized at 4 precisely
because a runaway suite once ate 36GB here. ``run_pr_tests`` honoured that:
``GateTestRunner._run_package`` wraps every pytest in ``self.pool.slot()``.

``run_required_checks`` did not. It passed only ``use_systemd`` down to
``run_checks``, and neither ``run_checks`` nor ``run_check`` mentioned a pool at
all, so a repo's ``dbt compile`` — an arbitrary, equally hungry build command —
ran with zero of the 4 slots held. Four concurrent gate runs on one repo meant
four simultaneous compiles, bounded by nothing but a per-process ``MemoryMax``
that does not exist at all on a host where ``systemd-run --user`` is
unavailable.

The count is read from the *real* pool by the check subprocess itself, so these
tests assert on the machine's actual slot state rather than on whether the gate
called ``pool.slot()``. A spy would pass against an implementation that took the
slot and dropped it before launching the command, which is the same bug wearing
a different hat: the budget is only a budget while the process that spends it is
alive.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, cast

import pytest

from agent_fleet.gate.checks import RequiredCheck, run_check, run_checks
from agent_fleet.slots import PoolConfig, SlotPoolFull
from agent_fleet.slots import test_slot_pool as make_test_pool

#: Prints the number of test-pool slots held, read from the pool rooted at
#: ``{root}``. Run as a check command, so it observes the slot state *while the
#: check subprocess is alive* — the only window in which the budget means
#: anything. Written to a file and invoked as ``python <file>``: a check command
#: is ``shlex.split`` before it runs, so an inline ``-c`` program with quotes in
#: it would be mangled by the same parsing the test is not about.
_PROBE_SRC = """
import sys
from pathlib import Path
sys.path.insert(0, {src_root!r})
from agent_fleet.slots import PoolConfig, test_slot_pool
pool = test_slot_pool(PoolConfig(root=Path({root!r}), agent_slots=0, test_slots={size}))
print("HELD", pool.in_use())
"""


def _pool(root: Path, size: int = 1) -> Any:  # noqa: ANN401
    return make_test_pool(
        PoolConfig(root=root, agent_slots=0, test_slots=size, poll_interval_s=0.01)
    )


def _probe(root: Path, size: int = 1) -> str:
    """A check command that reports live slot usage against the pool at *root*."""
    src_root = str(Path(__file__).resolve().parent.parent)
    script = root.parent / "probe_slot.py"
    root.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        _PROBE_SRC.format(src_root=src_root, root=str(root), size=size), encoding="utf-8"
    )
    return f"{sys.executable} {script}"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, timeout=60, capture_output=True)


@pytest.fixture
def slots_root(tmp_path: Path) -> Path:
    return tmp_path / "slots"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo whose ``pr`` branch adds ``transform/m.sql`` over ``main``.

    Selection is a real ``git diff`` of the branch under review against the
    config's base branch, so the fixture has to put the change *on a branch* and
    leave the checkout elsewhere — a commit made directly on ``main`` produces
    an empty diff and every check is legitimately selected by nothing.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git(tmp_path, "init", "-q", "-b", "main", str(root))
    _git(root, "config", "user.email", "gate@test.local")
    _git(root, "config", "user.name", "Gate Test")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")

    _git(root, "checkout", "-q", "-b", "pr")
    (root / "transform").mkdir()
    (root / "transform" / "m.sql").write_text("select 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "pr change")
    # Back on main: the diff of interest is main..pr, and a pipeline pointed at
    # the main checkout must still see it.
    _git(root, "checkout", "-q", "main")
    return root


def test_a_check_holds_a_test_slot_while_it_runs(slots_root: Path, repo: Path) -> None:
    """The check subprocess must observe its own slot held.

    A pool of one, so the number is unambiguous: anything other than ``HELD 1``
    means the budget was not in force during the run.
    """
    result = run_check(
        RequiredCheck(name="probe", command=_probe(slots_root)),
        worktree=repo,
        stage="head",
        changed_files=[],
        changed_models=[],
        pool=_pool(slots_root),
    )

    assert result.passed, f"the probe check did not run: {result.reason}"
    assert "HELD 1" in result.evidence, (
        f"the check ran with no slot held, so the test pool is bypassed: {result.evidence!r}"
    )


def test_a_second_check_waits_for_the_slot_rather_than_overlapping(
    slots_root: Path, repo: Path
) -> None:
    """Two checks cannot run at once in a pool of one — the second queues.

    A slot taken and released around the subprocess would let both overlap
    whenever the timings lined up; holding it for the whole run is what makes the
    budget a budget. This is the exact window the 36GB runaway needed, so it is
    exercised with a real concurrent second check rather than asserted about the
    lock file.
    """
    pool = _pool(slots_root)
    errors: list[BaseException] = []

    def _slow() -> None:
        try:
            run_check(
                RequiredCheck(
                    name="slow",
                    command=f'{sys.executable} -c "import time; time.sleep(2); print(1)"',
                ),
                worktree=repo,
                stage="head",
                changed_files=[],
                changed_models=[],
                pool=pool,
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=_slow)
    thread.start()
    # Bounded spin: wait until the first check has actually taken the only slot.
    # A broken implementation would never take it, so the loop has its own exit.
    for _ in range(2000):
        if pool.in_use() == 1:
            break
        threading.Event().wait(0.01)
    assert pool.in_use() == 1, "the first check never took the only slot"

    second = run_check(
        RequiredCheck(name="second", command=_probe(slots_root)),
        worktree=repo,
        stage="head",
        changed_files=[],
        changed_models=[],
        pool=pool,
    )
    thread.join(timeout=30)

    assert not thread.is_alive(), "the first check did not finish"
    assert not errors, f"the first check raised: {errors[0]}"
    # The second check ran, and by then the pool was free — which is the
    # queuing behaviour the budget requires, not an overlap.
    assert second.passed, f"the second check did not run: {second.reason}"
    assert "HELD 1" in second.evidence, f"unexpected probe output: {second.evidence!r}"


def test_a_full_pool_raises_rather_than_running_uncapped(slots_root: Path, repo: Path) -> None:
    """With no slot available, acquisition fails loudly instead of proceeding.

    Running anyway is the whole defect, so a caller that cannot get a slot must
    see that rather than silently proceed unbounded. The gate itself waits
    unboundedly, which is what makes this safe in production; a check that could
    hang forever is the one behaviour a memory cap must never have.
    """
    pool = _pool(slots_root)
    with pool.slot(timeout_s=5), pytest.raises(SlotPoolFull):
        _pool(slots_root).acquire(timeout_s=0.0)

    # And the guard is released afterwards, so a pool of one is not a deadlock.
    after = run_check(
        RequiredCheck(name="after", command=f'{sys.executable} -c "print(1)"'),
        worktree=repo,
        stage="head",
        changed_files=[],
        changed_models=[],
        pool=pool,
    )
    assert after.passed


def test_run_checks_holds_a_slot_for_every_selected_check(slots_root: Path, repo: Path) -> None:
    """The pool reaches each check, not only the first.

    A repo typically configures a compile *and* a lint; a guard applied to the
    first and not the second leaves half the run unbounded, which is the shape
    the defect took.
    """
    results = run_checks(
        [
            RequiredCheck(name="first", command=_probe(slots_root)),
            RequiredCheck(name="second", command=_probe(slots_root)),
        ],
        worktree=repo,
        stage="head",
        changed_files=["transform/m.sql"],
        pool=_pool(slots_root),
    )

    assert [r.name for r in results] == ["first", "second"]
    for r in results:
        assert r.passed, f"{r.name} did not run: {r.reason}"
        assert "HELD 1" in r.evidence, f"{r.name} ran with no slot held: {r.evidence!r}"


def _pipeline(repo: Path, gate_dir: Path, pool: Any, command: str) -> Any:  # noqa: ANN401
    from agent_fleet.gate.config import load_gate_config
    from agent_fleet.gate.pipeline import GatePipeline
    from agent_fleet.model_policy import parse_model_policy

    config = load_gate_config(
        {"gate": {"required_checks": [{"name": "probe", "command": command}]}}
    )
    assert config is not None, "gate must not be disabled by this config"
    return GatePipeline(
        repo=repo,
        pr_number=1,
        config=config,
        policy=parse_model_policy({}),
        backend=cast("Any", None),
        gate_dir=gate_dir,
        test_pool=pool,
        use_systemd=False,
    )


@pytest.fixture
def worktree(repo: Path, tmp_path: Path) -> Path:
    """A worktree checked out at the PR head, the way the gate builds one.

    ``changed_paths`` reads ``<base>...HEAD`` *in the worktree*, so the pipeline
    has to be pointed at a tree whose HEAD is the PR branch. Handing it the repo
    on ``main`` would select nothing and the test would pass for the wrong
    reason.
    """
    from agent_fleet.gate.gitops import prepare_worktree

    wt = tmp_path / "wt"
    prepare_worktree(repo, wt, "pr")
    return wt


def test_the_pipeline_runs_its_checks_through_the_test_pool(
    slots_root: Path, repo: Path, worktree: Path, tmp_path: Path
) -> None:
    """The pipeline must pass its test pool down, not merely accept one.

    ``run_check`` honouring a pool is only half the fix. The defect was that the
    pipeline never supplied one, so every real gate run still ran repo builds
    uncapped. This drives the real :meth:`GatePipeline.run_required_checks`
    against a real diff with a pool of one, and the check is asserted to have
    observed it — the wiring, not the capability.
    """
    pipeline = _pipeline(repo, tmp_path / "gate", _pool(slots_root), _probe(slots_root))
    results = pipeline.run_required_checks(worktree, stage="head")

    assert [r.name for r in results] == ["probe"], f"the check did not run: {results}"
    assert "HELD 1" in results[0].evidence, (
        f"the pipeline ran its check with no slot held: {results[0].evidence!r}"
    )


def test_a_pipeline_without_a_test_pool_still_runs_the_check(
    repo: Path, worktree: Path, tmp_path: Path
) -> None:
    """No pool means no guard, not a refusal.

    A caller that wires no pool — an older caller, a test fixture — must get a
    working check, still memory-capped. Refusing to run would turn a missing
    guard into a broken gate, which is a worse failure than the one being fixed.
    """
    pipeline = _pipeline(repo, tmp_path / "gate", None, f'{sys.executable} -c "print(1)"')
    (result,) = pipeline.run_required_checks(worktree, stage="head")
    assert result.passed, f"a check with no pool refused to run: {result.reason}"
