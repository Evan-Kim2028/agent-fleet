"""``run_lane`` must return a ``LaneRunResult``, not blow up on config.

``run_lane`` builds its :class:`AdmissionConfig` from ``config.admission`` before
the engine ``try``/``except``, so a ``FleetOpsConfig`` that has no ``admission``
field turns *every* lane run into an ``AttributeError`` that escapes
``run_lane`` altogether — which takes out the lane manager and the
``fleet lane run`` CLI with it. The admission budget is a throttle: a missing
knob has to fall back to the defaults, never abort the lane.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable  # noqa: TC003
from pathlib import Path
from typing import Any

import pytest

from agent_fleet.fleet_ops import admission as admission_mod
from agent_fleet.fleet_ops.admission import AdmissionConfig
from agent_fleet.fleet_ops.config import FleetOpsConfig, OperatorSpec
from agent_fleet.fleet_ops.runner import LaneRunResult, run_lane

#: A cmd JSONL stream that looks like real work: tool calls, then a result.
STREAM = "\n".join(
    [
        json.dumps({"type": "tool_completed", "subtype": "completed"}),
        json.dumps({"type": "tool_completed", "subtype": "completed"}),
        json.dumps({"type": "result", "finalText": "opened the PR"}),
    ]
)

HEAD_SHA = "a" * 40


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):  # noqa: ANN001, ANN202
    """Never touch the real ~/.agent-fleet registry from a test."""
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real repo with one commit on main and a real local bare origin."""
    root = tmp_path / "lake-of-rage"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "l@example.com")
    _git(root, "config", "user.name", "L")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "base")

    origin = tmp_path / "origin.git"
    origin.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        capture_output=True,
        text=True,
        check=True,
    )
    _git(root, "remote", "add", "origin", str(origin))
    _git(root, "push", "-u", "origin", "main")
    return root


@pytest.fixture
def task_file(tmp_path: Path) -> Path:
    path = tmp_path / "task.md"
    path.write_text("# Fix the thing\n\nDo the work.\n", encoding="utf-8")
    return path


def _lane_runner(
    *,
    head_ref: str = "fb/movers",
    engine_rc: int = 0,
    slug: str = "Evan-Kim2028/lake-of-rage",
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """A runner that fakes ``gh``, ``git remote get-url`` and the agent binary.

    Everything else — the commit, the hooks, the push, ``rev-list`` — runs for
    real against the tmp repo, because those are the behaviours worth testing.
    The fake also *remembers* a PR that ``gh pr create`` produced, so a later
    ``gh pr list`` finds it.
    """
    created: dict[str, int | None] = {"number": None}

    def runner(args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        argv = list(args)
        if argv[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(argv, 0, f"git@github.com:{slug}.git\n", "")
        if argv[:1] == ["gh"]:
            if argv[:3] == ["gh", "pr", "list"]:
                number = created["number"]
                payload = (
                    [{"number": number, "headRefName": head_ref, "headRefOid": HEAD_SHA}]
                    if number
                    else []
                )
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
            if argv[:3] == ["gh", "pr", "create"]:
                created["number"] = created["number"] or 3544
                return subprocess.CompletedProcess(
                    argv, 0, f"https://github.com/o/r/pull/{created['number']}\n", ""
                )
        if any(str(a).endswith("cmd") for a in argv[:3]) or "--max-turns" in argv:
            return subprocess.CompletedProcess(argv, engine_rc, STREAM, "")
        return subprocess.run(argv, **kwargs)

    return runner


def _run(repo: Path, task: Path, tmp_path: Path, **kwargs: Any) -> LaneRunResult:  # noqa: ANN401
    """Run a lane with the test's paths, merging *kwargs* over the defaults."""
    opts: dict[str, Any] = {
        "operator": "documents-1d",
        "lane": "movers",
        "repo_path": repo,
        "task_file": task,
        "engine": "cmd",
        "config": FleetOpsConfig(operators={"documents-1d": OperatorSpec(name="documents-1d")}),
        "status_file": tmp_path / "lane.status",
        "run_dir": tmp_path / "runs",
        "worktree_parent": tmp_path / "wt",
        "known_gate_subcommands": {"run"},
        "runner": _lane_runner(),
        "gate": False,
    }
    return run_lane(**{**opts, **kwargs})


def test_run_lane_returns_a_result_when_config_has_no_admission_field(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    """A default ``FleetOpsConfig`` carries no admission knobs; the lane must run.

    The admission budget is a throttle with sane defaults. Reading the missing
    ``config.admission`` must not escape ``run_lane`` as an ``AttributeError``
    before the engine's ``try``/``except`` ever runs.
    """
    try:
        result = _run(repo, task_file, tmp_path)
    except AttributeError as exc:
        pytest.fail(
            "run_lane raised "
            f"AttributeError({exc!r}) instead of returning a LaneRunResult: "
            "run_lane reads config.admission before the engine try/except, but "
            "FleetOpsConfig has no 'admission' field."
        )

    assert isinstance(result, LaneRunResult)
    # The lane actually ran: the engine started and finished. The fake engine
    # commits nothing, so a no-work verdict is correct downstream behaviour —
    # what matters is that the lane got that far instead of dying on the
    # admission lookup, which happens before any of these events are emitted.
    assert "lane.engine.start" in result.events
    assert "lane.engine.done" in result.events
    # The specific no-work reason is main's lazy-exit taxonomy, not this test's
    # subject: the fake engine leaves an account and no commits, so the lane ends
    # in ``no_changes_stopped`` (main) rather than the older ``no_commits_ahead``.
    # Either reason proves the lane survived the admission lookup, which is the
    # behaviour under test here.
    assert result.reason in {"no_changes_stopped", "no_commits_ahead"}


def test_run_lane_does_not_raise_for_any_operator_on_a_default_config(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    """The failure is config-shaped, not operator-shaped: any operator blows up.

    An operator name absent from the config is the documented "no spec" case, so
    this is the least-privileged path through ``run_lane`` — if the admission
    lookup is safe here it is safe everywhere.
    """
    for operator in ("documents-0e", "no-such-operator"):
        try:
            result = _run(repo, task_file, tmp_path, operator=operator)
        except AttributeError as exc:
            pytest.fail(
                f"run_lane(operator={operator!r}) raised AttributeError({exc!r}) "
                "instead of returning a LaneRunResult: config.admission is read "
                "before the engine try/except and does not exist."
            )
        assert isinstance(result, LaneRunResult)


def test_a_wedged_pool_gives_up_after_the_configured_budget(
    repo: Path, task_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A saturated test pool must cost the operator's budget, not the default.

    ``run_lane`` rebuilds its :class:`AdmissionConfig` from ``config.admission``
    before it hands the config to :func:`shim_env`, so a knob it fails to copy
    never reaches the generated shim. With every slot held the lane's
    ``uv run pytest`` sits inside the shim's acquire loop, so a ``wait_s`` that
    silently reverted to :data:`DEFAULT_WAIT_S` parks the lane for an hour and
    the operator sees a hung agent instead of a lane that gave up on schedule.
    """
    captured: dict[str, str] = {}

    def fake_write(target: Path, *, real: str, config: AdmissionConfig) -> Path:
        captured["source"] = admission_mod._shim_source(real, config)
        return Path(target) / admission_mod.SHIM_NAME

    monkeypatch.setattr(admission_mod, "write_shim", fake_write)

    config = FleetOpsConfig(
        operators={"documents-1d": OperatorSpec(name="documents-1d")},
        admission=AdmissionConfig(shared_dir=tmp_path, tests=1, wait_s=1.0),
    )
    result = _run(repo, task_file, tmp_path, config=config)

    assert isinstance(result, LaneRunResult)
    assert "source" in captured, "the engine child never got an admission shim"
    assert "WAIT_S = 1.0" in captured["source"]
    assert f"WAIT_S = {admission_mod.DEFAULT_WAIT_S!r}" not in captured["source"]


def test_the_repo_configured_wait_budget_reaches_the_generated_shim(
    repo: Path, task_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole ``fleet_ops.admission`` budget, not a hand-picked subset.

    ``run_lane`` is the only thing that builds the engine's shim env, so a knob
    it reconstructs field by field is a knob the operator cannot set. The repo
    config below sets all five, and every one of them has to survive into the
    source the shim actually runs.
    """
    rendered: list[str] = []
    real_write_shim = admission_mod.write_shim

    def capturing_write(target: Path, *, real: str, config: AdmissionConfig) -> Path:
        path = real_write_shim(target, real=real, config=config)
        rendered.append(path.read_text(encoding="utf-8"))
        return path

    monkeypatch.setattr(admission_mod, "write_shim", capturing_write)

    admission = AdmissionConfig(
        shared_dir=tmp_path / "pools",
        tests=7,
        typecheck=2,
        nice=3,
        wait_s=45.5,
    )
    config = FleetOpsConfig(
        operators={"documents-1d": OperatorSpec(name="documents-1d")},
        admission=admission,
    )
    result = _run(repo, task_file, tmp_path, config=config)

    assert isinstance(result, LaneRunResult)
    assert rendered, "the engine child never got an admission shim"
    source = rendered[-1]
    assert f"WAIT_S = {45.5!r}" in source
    assert f"NICE = {3!r}" in source
    assert f"{admission_mod.TEST_POOL!r}: {7}" in source
    assert f"{admission_mod.TYPECHECK_POOL!r}: {2}" in source
    assert str(tmp_path / "pools" / "slots") in source
