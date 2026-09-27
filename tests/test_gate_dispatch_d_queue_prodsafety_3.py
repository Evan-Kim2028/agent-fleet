"""prodsafety_3: the dispatcher's output root must stay inside AGENT_FLEET_HOME.

Claim under test
----------------
``dispatch_state_path`` documents why the operator is confined::

    It is passed through ``confined_name`` because it is untyped operator input
    -- joined in verbatim, a ``..`` or an absolute path writes outside this
    namespace.

``run_dispatch`` builds the same namespace a second time and does not confine
it::

    out_root = Path(run_dir).expanduser() if run_dir else dispatch_dir() / operator

so ``dispatch_dir() / operator`` takes the operator raw.  ``--operator`` is a
required flag with no validation in ``cmd_dispatch_queue``, and every task file,
status file, lane log and gate log is then written under that raw path -- while
the state file correctly lands in ``.../dispatch/<confined>/state.json``.  An
absolute operator escapes ``AGENT_FLEET_HOME`` outright.

The test runs the real ``run_dispatch`` without ``run_dir`` under an isolated
``AGENT_FLEET_HOME`` and asserts that everything the run writes stays under it.
Any implementation that confines the operator in ``out_root`` passes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet.fleet_ops import pressure
from agent_fleet.fleet_ops.dispatch import dispatch_dir, dispatch_state_path, run_dispatch

if TYPE_CHECKING:
    from collections.abc import Sequence

IDLE = pressure.Throttle(some_avg10=0.0, path=Path("/fake/cpu.pressure"), available=True)

#: Four ``..``: escapes ``.../lanes/dispatch`` and then the home itself.
EVIL_OPERATOR = "../../../../evil"


class _Proc:
    def __init__(self, pid: int, exit_code: int) -> None:
        self.pid = pid
        self.returncode = exit_code

    def poll(self) -> int | None:
        return self.returncode


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "afhome"
    home.mkdir()
    monkeypatch.setenv("AGENT_FLEET_HOME", str(home))


def test_everything_the_dispatcher_writes_stays_under_the_fleet_home(tmp_path: Path) -> None:
    """A traversal ``--operator`` must not redirect task/status/log writes.

    The state file is already confined; the output root is not, so the two
    disagree about where this dispatcher's namespace is. The test asserts the
    invariant the docstring claims: nothing lands outside ``AGENT_FLEET_HOME``.
    """
    home = tmp_path / "afhome"  # the AGENT_FLEET_HOME the fixture installed
    repo = tmp_path / "repo"
    repo.mkdir()
    queue = tmp_path / "q.jsonl"
    queue.write_text(json.dumps({"lane": "L1", "repo": "acme", "task": "t"}), encoding="utf-8")
    written: list[Path] = []

    def _spawn(_argv: Sequence[str], **kwargs: Any) -> Any:  # noqa: ANN401
        # The dispatcher hands spawn an open log handle (subprocess.Popen takes
        # only file objects for stdout), so the path under test is the handle's
        # own name.
        handle = kwargs["stdout"]
        stdout = Path(handle.name)
        written.append(stdout)
        stdout.parent.mkdir(parents=True, exist_ok=True)
        with handle as log:
            log.write('{"state": "no_pr"}')
        return _Proc(6666, 0)

    run_dispatch(
        operator=EVIL_OPERATOR,
        queue_path=queue,
        repos={"acme": str(repo)},
        max_lanes=2,
        max_gates=2,
        spawn=_spawn,
        psi_reader=lambda: IDLE,
        sleep=lambda _s: None,
    )

    home_root = home.resolve()
    escaped = sorted({p.resolve() for p in written if not p.resolve().is_relative_to(home_root)})
    state_path = dispatch_state_path(EVIL_OPERATOR)

    assert not escaped, (
        f"--operator {EVIL_OPERATOR!r} put log file(s) outside AGENT_FLEET_HOME "
        f"{home_root}: {[str(p) for p in escaped]}; out_root joins the raw operator "
        f"(= {dispatch_dir() / EVIL_OPERATOR}) while dispatch_state_path confines it "
        f"(= {state_path})"
    )
    assert state_path.resolve().is_relative_to(home_root), (
        f"control: the state file itself escaped: {state_path}"
    )
