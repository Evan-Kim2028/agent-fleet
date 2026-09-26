"""j-3: the gate is launched with neither --repo-path nor --task-file.

The gate subparser documents ``--repo-path`` ("Path to the git repo holding the
PR, default: cwd") and ``--task-file`` ("used as the yardstick by the spec lens
and the judge"). ``cmd_gate`` falls back to ``Path.cwd()`` when ``--repo-path``
is missing (cli.py:976) and passes ``task_file=None`` when ``--task-file`` is
missing (cli.py:978).

``gate_argv`` builds the default ``agent-fleet gate`` argv *without* either flag,
and the template path does not add them either. ``_launch_gate`` already holds
the correct checkout in ``record.repo_path`` (recorded at dispatch.py:1220) and
never passes it, so a multi-repo queue gates each PR against the dispatcher's
cwd rather than the repo that produced it, and judges it with no spec yardstick.

This test drives the real ``gate_argv`` (and, for the repo_path plumbing, the
real ``_launch_gate``) and asserts the gate argv carries ``--repo-path`` taken
from the record's repo_path and a ``--task-file``. At the current head these
assertions fail because neither flag is emitted.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_fleet.fleet_ops import dispatch as dispatch_mod
from agent_fleet.fleet_ops.dispatch import (
    DISPATCH_PR,
    DispatchItem,
    DispatchLane,
    DispatchState,
    LaunchGate,
    gate_argv,
    merge_queue,
)

if TYPE_CHECKING:
    import pytest

REPO = "/repos/acme"
WORKTREE = "/repos/acme-wt"


def _has_flag(argv: list[str], flag: str) -> bool:
    return flag in argv


def _flag_value(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


def test_default_gate_argv_carries_repo_path_and_task_file() -> None:
    """The built-in ``agent-fleet gate`` argv must name the repo and the spec.

    With no template the default argv is ``agent-fleet gate --lane ... --repo
    ... --pr ... --head-ref ...``. It must also carry ``--repo-path`` (the
    checkout holding the PR) and ``--task-file`` (the spec yardstick). At the
    current head neither is present, so the gate runs against the dispatcher's
    cwd and with no yardstick.
    """
    argv = gate_argv(
        None,
        lane="C0-fix",
        pr=42,
        repo="acme",
        operator="documents-0e",
        slug="Evan-Kim2028/acme",
        worktree=WORKTREE,
    )

    assert _has_flag(argv, "--repo-path"), (
        f"the default gate argv has no --repo-path: {argv}. cmd_gate falls back "
        "to Path.cwd(), so the gate reviews the wrong repository."
    )
    assert _has_flag(argv, "--task-file"), (
        f"the default gate argv has no --task-file: {argv}. The gate then judges "
        "the PR with no spec yardstick."
    )


def test_template_gate_argv_carries_repo_path_and_task_file() -> None:
    """A template-based gate command must also carry --repo-path/--task-file.

    The template path is the other branch of ``gate_argv``. It appends
    ``--worktree`` when a worktree is known, but likewise never supplies
    ``--repo-path`` or ``--task-file``, so a configured gate script gets the same
    wrong-repo / no-yardstick invocation.
    """
    argv = gate_argv(
        "/opt/fbgate {lane} {repo} {pr}",
        lane="C0-fix",
        pr=42,
        repo="acme",
        operator="documents-0e",
        slug="Evan-Kim2028/acme",
        worktree=WORKTREE,
    )

    assert _has_flag(argv, "--repo-path"), f"the template gate argv has no --repo-path: {argv}"
    assert _has_flag(argv, "--task-file"), f"the template gate argv has no --task-file: {argv}"


def test_launch_gate_passes_the_recorded_repo_path_to_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_launch_gate`` must hand the gate the repo checkout it recorded.

    ``_launch_lane`` records the checkout on the lane as ``record.repo_path``.
    ``_launch_gate`` must forward that to the gate as ``--repo-path`` so a
    multi-repo queue gates each PR against its own repo rather than the
    dispatcher's cwd. At the current head the recorded repo_path is never
    turned into a ``--repo-path`` argument.
    """
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    out_root = tmp_path / "out"

    item = DispatchItem.from_dict({"lane": "a", "repo": "acme", "task": "t", "ref": "R"})
    state = merge_queue(DispatchState(operator="op"), [item])
    # The lane has produced a PR and its checkout was recorded at launch.
    lanes = dict(state.lanes)
    lanes["a"] = DispatchLane(
        lane="a", item=item, state=DISPATCH_PR, pr=42, repo_path=REPO, worktree=WORKTREE
    )
    state = DispatchState(operator="op", lanes=lanes)

    captured: list[list[str]] = []

    class _Proc:
        pid = 4242

        def poll(self) -> int | None:
            return None

    def _spawn(argv: list[str], **kwargs: Any) -> Any:  # noqa: ANN401
        captured.append(list(argv))
        log = kwargs.get("stdout")
        if log is not None:
            Path(str(log)).parent.mkdir(parents=True, exist_ok=True)
            Path(str(log)).write_text("", encoding="utf-8")
        return _Proc()

    dispatch_mod._launch_gate(
        state,
        LaunchGate(lane="a", pr=42),
        gate_cmd=None,
        judge_engine=None,
        out_root=out_root,
        spawn=_spawn,
    )

    assert captured, "_launch_gate spawned nothing; the run is vacuous"
    gate_argv_built = captured[0]
    assert _flag_value(gate_argv_built, "--repo-path") == REPO, (
        f"the gate argv does not carry the recorded repo_path {REPO!r}: {gate_argv_built}"
    )
    assert _has_flag(gate_argv_built, "--task-file"), (
        f"the gate argv has no --task-file: {gate_argv_built}"
    )
