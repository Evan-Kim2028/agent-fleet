"""A carried-over approval must still be judged by the gate's own archived test.

``docs/GATE.md`` states the contract of ``gate recheck``: it "re-runs the PR's
own changed tests **plus the archived gate tests** on the new head with the
current base merged in".

The archive is the only place a gate-written regression test survives a fixer
push (the fixer pushes product code, so the test file disappears from the PR
branch). If the recheck never lists the archive, then the one test that ever
*blocked* the PR is not run at all, and a rebased head that reintroduces the
defect inherits the approval anyway — reported in the reason line as "all N
test(s) green on the rebased head".
"""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from agent_fleet.gate import gitops
from agent_fleet.gate.pipeline import GateTestArchive, run_gate_recheck

if TYPE_CHECKING:
    from pathlib import Path

_PYPROJECT = """[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[project]
name = "recheck-archive-fixture"
version = "0.0.0"

[dependency-groups]
dev = ["pytest"]

[tool.setuptools]
py-modules = ["agent", "limits"]

[tool.pytest.ini_options]
pythonpath = ["."]
"""

#: The gate's archived regression test. Green on the approved head — the PR
#: fixed the defect the gate confirmed, and ``main``'s limit is what the test
#: pins — and red on the rebased head, where ``main`` moved that limit under it.
_GATE_TEST = (
    "from agent import VALUE\nfrom limits import MAX\n\n\n"
    "def test_gate_fb_lane_c_1():\n    assert (VALUE, MAX) == (2, 10)\n"
)

#: The PR's own test: still green on the rebased head, which is the whole point
#: — the recheck's own test set cannot see the regression.
_PR_TEST = "from agent import VALUE\n\n\ndef test_pr():\n    assert VALUE == 2\n"

_GATE_TEST_REL = "tests/test_gate_fb_lane_c_1.py"
_PR_TEST_REL = "tests/test_pr.py"


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return out.stdout.strip()


def _commit(repo: Path, message: str, name: str, content: str) -> str:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


class _Repo:
    """A ``main``, a PR branch, and a way to move main and rebase onto it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        _git(root, "init", "-q", "-b", "main", str(root))
        _git(root, "config", "user.email", "gate@test.local")
        _git(root, "config", "user.name", "Gate Test")
        (root / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
        (root / "limits.py").write_text("MAX = 10\n", encoding="utf-8")
        (root / "agent.py").write_text("VALUE = 1\n", encoding="utf-8")
        (root / "tests").mkdir()
        (root / _PR_TEST_REL).write_text(
            "from agent import VALUE\n\n\ndef test_pr():\n    assert VALUE > 0\n", encoding="utf-8"
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "base")
        _git(root, "checkout", "-q", "-b", "fb/lane")
        # The PR's change: it fixes the confirmed defect by moving VALUE, and
        # its own test pins that fix. It says nothing about the limit the gate's
        # own archived test also pins.
        self.approved = _commit(self.root, "pr change", "agent.py", "VALUE = 2\n")
        self.approved = _commit(self.root, "pr test", _PR_TEST_REL, _PR_TEST)

    def move_main(self, commit: tuple[str, str, str]) -> None:
        current = _git(self.root, "rev-parse", "--abbrev-ref", "HEAD")
        _git(self.root, "checkout", "-q", "main")
        _commit(self.root, *commit)
        _git(self.root, "checkout", "-q", current)

    def rebase(self) -> str:
        _git(self.root, "rebase", "main")
        return _git(self.root, "rev-parse", "HEAD")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the agent-fleet home (metrics, slots) and the fleet config."""
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("AGENT_FLEET_HOME", str(root))
    monkeypatch.setenv("AGENT_FLEET_CONFIG", str(root / "absent.yaml"))
    return root


@pytest.fixture
def setup(home: Path, tmp_path: Path) -> dict[str, Any]:
    """A PR approved with a confirmed gate test archived, then rebased.

    After the rebase ``main`` reintroduces the defect the gate confirmed, so
    the archived test is red on the new head: running it is the difference
    between carrying the approval over and demanding a full gate.
    """
    repo = _Repo(tmp_path / "repo")
    gate_dir = tmp_path / "gate"
    (gate_dir / "tests").mkdir(parents=True)
    (gate_dir / "tests" / "test_gate_fb_lane_c_1.py").write_text(_GATE_TEST, encoding="utf-8")

    status = tmp_path / "lane.status"
    status.write_text(f"10:00:00 PREMERGE-APPROVED {repo.approved}\n", encoding="utf-8")

    config = home / "fleet.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "gate": {
                    "lane_slug": "fb_lane",
                    "test_slots": 4,
                    "test_memory": "6G",
                    "test_timeout_s": 300,
                }
            }
        ),
        encoding="utf-8",
    )

    # ``main`` moves the limit the gate's archived test pins. The PR's own diff
    # (agent.py) is untouched, so the rebase is clean and patch-identical — but
    # the state the approved change was judged against is gone.
    repo.move_main(("main moves on", "limits.py", "MAX = 12\n"))
    head = repo.rebase()
    return {
        "repo": repo,
        "gate_dir": gate_dir,
        "status": status,
        "config": config,
        "approved": repo.approved,
        "head": head,
    }


def _recheck(setup: dict[str, Any]) -> Any:  # noqa: ANN401
    return run_gate_recheck(
        repo_path=setup["repo"].root,
        pr_number=7,
        approved_sha=setup["approved"],
        head_sha=setup["head"],
        status_file=setup["status"],
        config_path=str(setup["config"]),
        gate_dir=setup["gate_dir"],
        use_systemd=False,
    )


# ---------------------------------------------------------------------------
# The fixture has to be a real failure, or the assertions below prove nothing
# ---------------------------------------------------------------------------


def test_the_rebase_is_patch_identical(setup: dict[str, Any]) -> None:
    assert gitops.patch_id(setup["repo"].root, setup["approved"], "main") == gitops.patch_id(
        setup["repo"].root, setup["head"], "main"
    ), "the carry-over needs a patch-identical rebase, not a different change"


def test_the_gate_test_is_not_part_of_the_prs_own_changed_tests(setup: dict[str, Any]) -> None:
    """The archive is the only place this test exists — the gate's evidence,
    not the PR's change, so ``changed_test_files`` can never name it."""
    assert not (setup["repo"].root / _GATE_TEST_REL).exists()
    worktree = setup["repo"].root
    assert _GATE_TEST_REL not in gitops.changed_test_files(worktree, "main")
    assert _PR_TEST_REL in gitops.changed_test_files(worktree, "main")


def _head_checkout(setup: dict[str, Any], tag: str) -> Path:
    """A detached checkout of the rebased head, with the archived test in it."""
    path = setup["repo"].root.parent / f"head-{tag}"
    _git(setup["repo"].root, "worktree", "add", "--detach", "-q", str(path), setup["head"])
    shutil.copy2(setup["gate_dir"] / "tests" / "test_gate_fb_lane_c_1.py", path / _GATE_TEST_REL)
    return path


def test_the_archived_gate_test_fails_on_the_rebased_head(setup: dict[str, Any]) -> None:
    """Guard: the archived test really is red on the rebased head, and the
    PR's own test is green there — otherwise the assertions below prove
    nothing."""
    from agent_fleet.gate.pytest_runner import run_pytest

    gate_wt = _head_checkout(setup, "gate")
    pr_wt = _head_checkout(setup, "pr")
    try:
        gate_outcome = run_pytest(gate_wt, [_GATE_TEST_REL], timeout_s=300)
        pr_outcome = run_pytest(pr_wt, [_PR_TEST_REL], timeout_s=300)
    finally:
        for wt in (gate_wt, pr_wt):
            _git(setup["repo"].root, "worktree", "remove", "--force", str(wt))
    assert gate_outcome.tests_failed, f"the archived gate test must be red: {gate_outcome.stdout}"
    assert pr_outcome.passed, f"the PR's own test must be green: {pr_outcome.stdout}"


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


def test_recheck_runs_the_archived_gate_test(setup: dict[str, Any]) -> None:
    """The documented contract: recheck re-runs the PR's changed tests *plus*
    the archived gate tests, and the verdict names the ones that failed."""
    result = _recheck(setup)
    assert any("test_gate_fb_lane_c_1" in reason for reason in result.reasons), (
        f"the archived gate test was never run; reasons: {result.reasons}"
    )


def test_recheck_refuses_when_an_archived_gate_test_fails(setup: dict[str, Any]) -> None:
    """The consequence: a head that reintroduces a confirmed defect must not
    inherit the approval the archived test once earned away."""
    result = _recheck(setup)
    assert not result.approved, (
        "an approval was carried onto a head where the archived gate test fails; "
        f"reasons: {result.reasons}"
    )
    assert any("full gate required" in reason for reason in result.reasons), result.reasons


def test_the_archive_has_a_listing_the_recheck_could_have_used(setup: dict[str, Any]) -> None:
    """``GateTestArchive`` can store and materialise a test but cannot report
    which tests it holds, so the recheck's ``pipeline.evidence.gate_tests`` is
    the only input it consults — and a fresh pipeline's is always empty."""
    archive = GateTestArchive(setup["gate_dir"])
    assert (archive.dir / "test_gate_fb_lane_c_1.py").is_file()
    worktree = setup["gate_dir"] / "wt"
    worktree.mkdir()
    # With an empty gate_tests list the archive materialises nothing.
    assert archive.materialise(worktree, []) == []
    assert not (worktree / _GATE_TEST_REL).exists()
