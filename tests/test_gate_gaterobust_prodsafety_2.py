"""An approval must not carry over a change that only exists in a gate-named file.

``GATE_TEST_EXCLUDE`` blanks ``:(exclude,glob)**/test_gate_*.py`` out of the
diff that ``patch_id`` hashes. The glob is name-based, so it does not just skip
the gate's own evidence — it skips *any* path that happens to be called
``test_gate_*.py`` anywhere in the tree, including product code. A commit whose
entire content is confined to such a path is therefore invisible to patch
identity: ``patch_id`` returns the same hash as the approved commit, and
``recheck_carry_over`` sees a patch-identical rebase and approves a head nobody
ever reviewed.

The safety property under test: the carry-over decision must distinguish
"re-parented, same change" from "same reviewed change *plus* unreviewed new
code". Blanket-approving the second is exactly the failure an approval
carry-over exists to prevent.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from agent_fleet.contracts.gate import GateOutcome
from agent_fleet.gate.config import GateConfig
from agent_fleet.gate.gitops import patch_id
from agent_fleet.gate.pipeline import GatePipeline, TestRun
from agent_fleet.model_policy import ModelPolicy

if TYPE_CHECKING:
    from pathlib import Path

_PYPROJECT = """[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[project]
name = "prodsafety-2-fixture"
version = "0.0.0"
"""

#: A product file that merely *matches* the gate-evidence naming glob.
_BACKDOOR_PATH = "src/pkg/test_gate_prod.py"


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return out.stdout.strip()


class _Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        _git(root, "init", "-q", "-b", "main", str(root))
        _git(root, "config", "user.email", "gate@test.local")
        _git(root, "config", "user.name", "Gate Test")
        (root / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
        (root / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "base")
        _git(root, "checkout", "-q", "-b", "fb/gaterobust")
        # The reviewed change: product code, plus a gate-named file that was
        # already on the approved head.
        (root / "a.py").write_text("VALUE = 2\n", encoding="utf-8")
        target = root / _BACKDOOR_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("SAFE = 1\n", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "reviewed change")
        self.approved = self.head()

    def head(self) -> str:
        return _git(self.root, "rev-parse", "HEAD")

    def commit(self, message: str, name: str, content: str) -> str:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", message)
        return self.head()

    def move_main(self, content: str) -> None:
        current = _git(self.root, "rev-parse", "--abbrev-ref", "HEAD")
        _git(self.root, "checkout", "-q", "main")
        self.commit("main moves on", "other.py", content)
        _git(self.root, "checkout", "-q", current)

    def rebase(self) -> str:
        _git(self.root, "rebase", "main")
        return self.head()


@pytest.fixture
def repo(tmp_path: Path) -> _Repo:
    return _Repo(tmp_path / "repo")


def _status_with_approval(tmp_path: Path, sha: str) -> Path:
    status = tmp_path / "lane.status"
    status.write_text(f"10:00:00 PREMERGE-APPROVED {sha}\n", encoding="utf-8")
    return status


def _recheck(repo: _Repo, tmp_path: Path, status: Path, head: str):  # noqa: ANN202
    pipe = GatePipeline(
        repo=repo.root,
        pr_number=7,
        config=GateConfig(backend="cmd", model="m", judge_backend="cmd", enable_judge=False),
        policy=ModelPolicy(backends={}),
        backend=object(),  # type: ignore[arg-type]
        gate_dir=tmp_path / "gate",
        status_file=status,
        use_systemd=False,
        lane_slug="fb_gaterobust",
    )
    pipe.evidence.gate_tests = []
    return pipe.recheck_carry_over(
        approved_sha=repo.approved,
        head_sha=head,
        status_file=status,
        test_run=TestRun(failing=[], ran=1),
        test_files=[],
    )


def test_patch_id_sees_new_code_hidden_in_a_gate_named_file(repo: _Repo) -> None:
    """The identity check must not call these two heads the same change."""
    repo.move_main("UNRELATED = 1\n")
    repo.rebase()
    new = repo.commit(
        "unreviewed code lands",
        _BACKDOOR_PATH,
        "SAFE = 1\nBACKDOOR = 1\n",
    )
    assert new != repo.approved
    assert patch_id(repo.root, repo.approved, "main") != patch_id(repo.root, new, "main"), (
        "a head whose only difference from the approved commit is new, "
        "never-reviewed code in a gate-named file must not be patch-identical"
    )


def test_recheck_does_not_approve_a_head_whose_only_new_code_is_invisible(
    repo: _Repo, tmp_path: Path
) -> None:
    """End to end: the unreviewed commit must force a full gate, not an approval."""
    status = _status_with_approval(tmp_path, repo.approved)
    repo.move_main("UNRELATED = 1\n")
    repo.rebase()
    new = repo.commit(
        "unreviewed code lands",
        _BACKDOOR_PATH,
        "SAFE = 1\nBACKDOOR = 1\n",
    )
    result = _recheck(repo, tmp_path, status, new)
    assert result.outcome is GateOutcome.NEEDS_ESCALATION, (
        f"carry-over approved head {new[:9]}, whose only difference from the "
        f"approved commit is unreviewed code in {_BACKDOOR_PATH}: {result.reasons}"
    )
    assert result.approved is False
