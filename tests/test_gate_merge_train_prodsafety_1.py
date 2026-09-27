"""A ``deploy_units`` freeze must stop ``merge train`` from landing a PR.

Claim (prodsafety-1): ``_held_batch`` hard-codes ``deploy_unit=\"\"`` when
matching cluster holds, so a hold configured with only ``match.deploy_units``
(a first-class, documented hold form) never matches and ``merge train`` merges
the very PRs the operator froze.  The train's own docstring promises \"a freeze
declared for merge run is a freeze for the train too\", and ``execute.py``
resolves the deploy unit per PR, so the data is available and simply unused.

This drives the whole ``cmd_merge_train`` entry point over a real git checkout
with a real fleet.yaml, a real hold ledger, and a real approval — and stubs only
``gh`` (so nothing is actually merged on GitHub) and the outer ``run_train``
network layer.  The assertion is on whether the batch was handed to
``run_train`` at all: today it is, which is exactly the bypass.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from typing import TYPE_CHECKING, Any

from agent_fleet.merge_plan.collect import GitHubClient
from agent_fleet.merge_plan.train import TrainResult

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.strip()


def _repo_with_dbt_pr(tmp_path: Path) -> tuple[Path, str]:
    """A checkout on ``main`` with one PR touching a dbt (transform/) model."""
    upstream = tmp_path / "upstream"
    origin = tmp_path / "origin.git"
    clone = tmp_path / "clone"
    (upstream / "transform" / "models").mkdir(parents=True)
    _git(upstream, "init", "-q", "-b", "main")
    _git(upstream, "config", "user.email", "t@example.com")
    _git(upstream, "config", "user.name", "T")
    (upstream / "transform" / "models" / "base.sql").write_text("select 1\n", encoding="utf-8")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-q", "-m", "base")
    _git(upstream, "clone", "-q", "--bare", ".", str(origin))
    _git(upstream, "remote", "add", "origin", str(origin))
    _git(upstream, "push", "-q", "origin", "main")
    _git(upstream, "clone", "-q", str(origin), str(clone))
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "T")

    _git(upstream, "checkout", "-q", "-b", "feat/orders", "main")
    (upstream / "transform" / "models" / "orders.sql").write_text("select 2\n", encoding="utf-8")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-q", "-m", "orders model")
    head = _git(upstream, "rev-parse", "HEAD")
    _git(upstream, "push", "-q", "origin", "feat/orders")
    return clone, head


def _freeze_config(tmp_path: Path, checkout: Path) -> Path:
    """A fleet.yaml freezing the ``dbt`` deploy unit, with the units declared."""
    config = tmp_path / "fleet.yaml"
    config.write_text(
        "merge_plan:\n"
        "  executor:\n"
        f"    state_dir: {tmp_path / 'state'}\n"
        "    holds:\n"
        "      - name: dbt-freeze\n"
        "        match:\n"
        "          deploy_units: ['dbt']\n"
        "  repos:\n"
        "    - name: demo\n"
        f"      path: {checkout}\n"
        "      deploy_units:\n"
        "        transform/models/: dbt\n",
        encoding="utf-8",
    )
    return config


def _status_file(tmp_path: Path, number: int, sha: str) -> Path:
    status = tmp_path / "status"
    status.mkdir(exist_ok=True)
    (status / "gate.md").write_text(
        f"Evan-Kim2028/demo#{number}\nPREMERGE-APPROVED {sha}\n", encoding="utf-8"
    )
    return status


class _DbtDetailClient:
    """Reports the approved PR as open, on a dbt file, with no lane name."""

    def __init__(self, head: str) -> None:
        self.head = head

    def pr_detail(self, pr_number: int) -> dict[str, Any]:
        del pr_number
        return {
            "state": "OPEN",
            "headRefOid": self.head,
            "headRefName": "fb/orders",
            "baseRefName": "main",
            "files": [{"path": "transform/models/orders.sql"}],
        }


def test_a_deploy_unit_freeze_prevents_merge_train_from_landing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The claimed defect: the batch reaches ``gh pr merge`` under a live freeze.

    The operator froze the ``dbt`` deploy unit.  ``merge run`` honours that by
    computing the batch's deploy unit and refusing.  ``merge train`` must reach
    the same decision; today it hard-codes an empty deploy unit, the hold cannot
    match, and the approved dbt PR is handed to the fold/merge path.
    """
    from agent_fleet.merge_plan import cli as merge_cli

    checkout, head = _repo_with_dbt_pr(tmp_path)
    config = _freeze_config(tmp_path, checkout)
    status = _status_file(tmp_path, 12, head)
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))

    monkeypatch.setattr(
        GitHubClient,
        "for_repo",
        _for_repo_returning(_DbtDetailClient(head)),
    )
    run_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "agent_fleet.merge_plan.train.run_train",
        lambda **kwargs: run_calls.append(kwargs) or _released_result(),
    )

    args = argparse.Namespace(
        repo_path=str(checkout),
        repo="demo",
        config=str(config),
        base_branch=None,
        operator=None,
        status_dir=str(status),
        test_command="pytest",
        max_batch_size=5,
        report=None,
        dry_run=False,
        json=False,
    )
    code = merge_cli.cmd_merge_train(args)

    err = capsys.readouterr().err
    assert run_calls == [], (
        "merge train ran the batch under an active 'dbt-freeze' hold; the freeze "
        f"on the dbt deploy unit was bypassed (exit code {code}, stderr: {err!r})"
    )
    assert "dbt-freeze" in err
    assert "#12" in err
    assert code == 1


def test_the_ledger_release_still_lets_a_deploy_unit_frozen_batch_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A released freeze must not block — proves the hold, not a blanket deny."""
    from agent_fleet.merge_plan import cli as merge_cli

    checkout, head = _repo_with_dbt_pr(tmp_path)
    config = _freeze_config(tmp_path, checkout)
    status = _status_file(tmp_path, 12, head)
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))

    # Release the hold in the ledger, exactly as ``fleet merge release`` writes it.
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "ledger.json").write_text(json.dumps({"released_holds": ["dbt-freeze"]}))

    monkeypatch.setattr(
        GitHubClient,
        "for_repo",
        _for_repo_returning(_DbtDetailClient(head)),
    )
    run_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "agent_fleet.merge_plan.train.run_train",
        lambda **kwargs: run_calls.append(kwargs) or _released_result(),
    )

    args = argparse.Namespace(
        repo_path=str(checkout),
        repo="demo",
        config=str(config),
        base_branch=None,
        operator=None,
        status_dir=str(status),
        test_command="pytest",
        max_batch_size=5,
        report=None,
        dry_run=False,
        json=False,
    )
    merge_cli.cmd_merge_train(args)
    assert len(run_calls) == 1, "a released dbt-freeze must not block the train"


def _released_result() -> TrainResult:
    return TrainResult(repo="demo", base_branch="main", detail="nothing landed")


def _for_repo_returning(
    client: _DbtDetailClient,
) -> Callable[[GitHubClient, Path], _DbtDetailClient]:
    """A ``GitHubClient.for_repo`` replacement handing back *client*."""

    def _factory(self: GitHubClient, repo_path: Path) -> _DbtDetailClient:
        del self, repo_path
        return client

    return _factory
