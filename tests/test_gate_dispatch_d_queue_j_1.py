"""j-1: unsanitised lane/operator names must not escape their intended roots.

``run_dispatch`` writes a lane's rendered task file to
``out_root/prompts/<lane>.task.md`` and its durable state to
``lanes/dispatch/<operator>/state.json``. Both names are interpolated verbatim
into the path, and ``DispatchItem.from_dict`` accepts any non-empty ``lane``
string, so a queue line whose lane contains ``..`` — or an ``--operator`` that
is absolute — redirects those writes outside the directory they are supposed to
be confined to. The module even defines ``_slugify`` (dispatch.py:272) for
exactly this and never calls it.

This test drives the real ``run_dispatch`` loop with an injected ``spawn`` (no
subprocesses) and traversing names, then asserts that every file the run wrote
is confined to the root it was supposed to be confined to, and that the
traversal is caught (not silently reported as a clean run). At the current head
these assertions fail: the writes land outside the root and ``summary.errors
== 0``, so the escape is silent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_fleet.fleet_ops import pressure
from agent_fleet.fleet_ops.dispatch import DispatchState, run_dispatch, save_state

IDLE = pressure.Throttle(some_avg10=0.0, path=Path("/fake/cpu.pressure"), available=True)


class _FakeProc:
    def __init__(self, pid: int, exit_code: int | None = None) -> None:
        self.pid = pid
        self.returncode = exit_code

    def poll(self) -> int | None:
        return self.returncode


def test_traversing_lane_name_stays_inside_run_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lane whose name walks out of ``run_dir`` must not write outside it.

    ``out_root`` is ``run_dir``; the task file goes to
    ``out_root/prompts/<lane>.task.md``. With ``lane='../../ESCAPED-lane'`` that
    resolves outside ``run_dir`` at the current head, silently (``errors == 0``).
    The correct behaviour is that every path the dispatcher writes — the
    ``--task-file``/``--status-file`` it hands the child, and the files on disk
    — stays confined to ``run_dir``.
    """
    # Isolate the durable dispatch state so the loop starts from a clean slate.
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    queue = sandbox / "q.jsonl"
    lane = "../../ESCAPED-lane"
    queue.write_text(json.dumps({"lane": lane, "repo": "acme", "task": "t"}), encoding="utf-8")
    repo = sandbox / "repo"
    repo.mkdir()
    out = sandbox / "out"

    seen: list[list[str]] = []

    def spawn(argv: list[str], **kwargs: Any) -> Any:  # noqa: ANN401, ARG001
        seen.append(list(argv))
        return _FakeProc(4242, 1)

    summary = run_dispatch(
        operator="documents-0e",
        queue_path=queue,
        repos={"acme": str(repo)},
        spawn=spawn,
        psi_reader=lambda: IDLE,
        sleep=lambda _s: None,
        run_dir=out,
    )

    # The escape is silent: a clean-looking run, not an error.
    assert summary.errors == 0, f"unexpected dispatch error: {summary.state}"
    assert seen, "run_dispatch spawned nothing; the run is vacuous"

    out_resolved = out.resolve()
    lane_argv = seen[0]

    def _flag_value(flag: str) -> str:
        return lane_argv[lane_argv.index(flag) + 1]

    # The paths the dispatcher chose for the traversing lane must be inside the
    # run directory. At the current head --task-file resolves outside it.
    for flag in ("--task-file", "--status-file"):
        chosen = Path(_flag_value(flag)).resolve()
        assert chosen.is_relative_to(out_resolved), (
            f"traversing lane name put {flag} outside run_dir: {chosen} (run_dir={out_resolved})"
        )


@pytest.mark.parametrize("operator", ["../ESCAPED-op", "../../ESCAPED-op", "..", "a/../../b"])
def test_operator_name_stays_inside_its_dispatch_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operator: str
) -> None:
    """A traversing/absolute ``--operator`` must not write state outside the dispatch dir.

    ``dispatch_state_path(operator)`` joins the raw operator string onto
    ``lanes/dispatch``. An operator containing ``..`` (or an absolute path)
    makes ``save_state`` mkdir -p and write the state file outside the
    per-operator namespace under ``dispatch/``; an absolute operator leaves
    ``AGENT_FLEET_HOME`` entirely. The correct behaviour is that the state file
    is confined to ``dispatch/<operator>/state.json``.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENT_FLEET_HOME", str(home))

    dispatch_root = home / "lanes" / "dispatch"
    state = DispatchState(operator=operator)
    written = save_state(state).resolve()

    assert written.is_relative_to(dispatch_root.resolve()), (
        f"operator {operator!r} wrote dispatch state outside {dispatch_root.resolve()}: {written}"
    )
