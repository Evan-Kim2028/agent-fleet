"""One approved PR must enter the train once, however its repo is spelled.

Claim (contract-1): ``cmd_merge_train`` de-duplicates approvals *before*
reconciling repo spellings::

    approvals = [
        a for a in dedupe_approvals(approvals) if normalize_repo(a, {repo: repo}).repo == repo
    ]

``dedupe_approvals`` keys on the raw ``(repo, pr_number)``, so a PR recorded by
the lane registry as ``lake-of-rage`` and by a status file as
``Evan-Kim2028/lake-of-rage`` has two different raw repo strings, survives the
dedupe as two entries, and the later ``normalize_repo`` filter keeps both — the
same PR enters the batch twice.  It is then folded, tested, and merged twice,
``batch_size`` reports 2 for one PR, and ``--max-batch-size`` admits fewer
distinct PRs than the cap.

This is the inverse of the contract ``build_plan`` was fixed for — ``plan.py``
normalises then de-dupes, and ``tests/test_gate_prodsafety_2.py`` pins it.

Drives the real ``cmd_merge_train`` over a real checkout with the real
collectors, stubbing only ``gh`` and the outer ``run_train``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from typing import TYPE_CHECKING, Any

from agent_fleet.merge_plan.collect import GitHubClient, collect_from_lanes, collect_from_status_dir
from agent_fleet.merge_plan.train import TrainResult

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

HEAD_SHA = "dc91fac7c9233ec3da9e9"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.strip()


def _checkout(tmp_path: Path) -> Path:
    upstream = tmp_path / "upstream"
    clone = tmp_path / "lake-of-rage"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _git(upstream, "config", "user.email", "t@example.com")
    _git(upstream, "config", "user.name", "T")
    (upstream / "a.txt").write_text("a\n", encoding="utf-8")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-q", "-m", "base")
    _git(upstream, "clone", "-q", str(upstream), str(clone))
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "T")
    return clone


def _two_spellings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One approved PR #42, recorded bare in lanes and owner-qualified in status."""
    from agent_fleet.merge_plan.collect import lanes_dir

    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    # Lane registry: the bare repo name.
    operator_dir = lanes_dir() / "Evan-Kim2028"
    operator_dir.mkdir(parents=True, exist_ok=True)
    (operator_dir / "lane-a.json").write_text(
        json.dumps(
            {
                "repo": "lake-of-rage",
                "pr": 42,
                "status_line": f"PREMERGE-APPROVED {HEAD_SHA}",
                "operator": "Evan-Kim2028",
            }
        ),
        encoding="utf-8",
    )
    # Status dir: the owner-qualified spelling of the same PR.
    status = tmp_path / "status"
    status.mkdir()
    (status / "gate.md").write_text(
        f"Evan-Kim2028/lake-of-rage#42\nPREMERGE-APPROVED {HEAD_SHA}\n", encoding="utf-8"
    )
    return status


class _Detail:
    def pr_detail(self, pr_number: int) -> dict[str, Any]:
        del pr_number
        return {
            "state": "OPEN",
            "headRefOid": HEAD_SHA,
            "headRefName": "fb/contract-1",
            "baseRefName": "main",
            "files": [{"path": "a.txt"}],
        }


def _install(monkeypatch: pytest.MonkeyPatch, calls: list[Any]) -> None:
    monkeypatch.setattr(GitHubClient, "for_repo", _for_repo_returning(_Detail()))
    monkeypatch.setattr(
        "agent_fleet.merge_plan.train.run_train",
        lambda **kwargs: calls.append(kwargs) or _result(),
    )


def _for_repo_returning(client: _Detail) -> Callable[[GitHubClient, Path], _Detail]:
    """A ``GitHubClient.for_repo`` replacement handing back *client*."""

    def _factory(self: GitHubClient, repo_path: Path) -> _Detail:
        del self, repo_path
        return client

    return _factory


def _result() -> TrainResult:
    return TrainResult(repo="lake-of-rage", base_branch="main", detail="nothing landed")


def _args(
    checkout: Path, config: Path, status: Path, *, max_batch_size: int = 5
) -> argparse.Namespace:
    return argparse.Namespace(
        repo_path=str(checkout),
        repo="lake-of-rage",
        config=str(config),
        base_branch=None,
        operator=None,
        status_dir=str(status),
        test_command="pytest",
        max_batch_size=max_batch_size,
        report=None,
        dry_run=False,
        json=True,
    )


def _config(tmp_path: Path, checkout: Path) -> Path:
    config = tmp_path / "fleet.yaml"
    config.write_text(
        f"merge_plan:\n  repos:\n    - name: lake-of-rage\n      path: {checkout}\n",
        encoding="utf-8",
    )
    return config


def test_one_approved_pr_enters_the_train_once_however_it_is_spelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claimed defect: the batch holds PR #42 twice."""
    from agent_fleet.merge_plan import cli as merge_cli

    checkout = _checkout(tmp_path)
    config = _config(tmp_path, checkout)
    status = _two_spellings(tmp_path, monkeypatch)
    calls: list[Any] = []
    _install(monkeypatch, calls)

    merge_cli.cmd_merge_train(_args(checkout, config, status))

    assert calls, "precondition: the train ran"
    batch = calls[0]["prs"]
    numbers = [p.number for p in batch]
    assert numbers == [42], (
        f"one approved PR entered the batch {len(numbers)} times as {numbers}; "
        "de-duping runs before normalize_repo, so the two repo spellings survive"
    )


def test_the_report_does_not_double_count_one_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``batch_size`` / ``ordered`` must count the PR once, not twice."""
    from agent_fleet.merge_plan import cli as merge_cli

    checkout = _checkout(tmp_path)
    config = _config(tmp_path, checkout)
    status = _two_spellings(tmp_path, monkeypatch)
    calls: list[Any] = []
    _install(monkeypatch, calls)
    tmp_path / "report.json"

    merge_cli.cmd_merge_train(_args(checkout, config, status))
    payload = json.loads(capsys.readouterr().out)

    # The trainer was handed one PR.  The run's own report must agree.
    assert payload["batch_size"] == len(calls[0]["prs"])
    assert len(set(payload["ordered"])) == len(payload["ordered"]), (
        f"the report's ordered list repeats a PR: {payload['ordered']}"
    )


def test_the_batch_cap_counts_distinct_prs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A duplicated PR must not eat a slot in ``--max-batch-size``.

    With the cap at 1, a batch that contains PR #42 twice is over the cap even
    though only one distinct PR is present — and a cap of 2 admits only one
    distinct PR when a second is approved alongside it.
    """
    from agent_fleet.merge_plan import cli as merge_cli

    checkout = _checkout(tmp_path)
    config = _config(tmp_path, checkout)
    status = _two_spellings(tmp_path, monkeypatch)
    calls: list[Any] = []
    _install(monkeypatch, calls)

    merge_cli.cmd_merge_train(_args(checkout, config, status, max_batch_size=1))

    assert calls, "precondition: the train ran"
    assert len(calls[0]["prs"]) == 1, (
        f"--max-batch-size 1 admitted {len(calls[0]['prs'])} batch entries; a single "
        "approved PR was counted twice against the cap"
    )


def test_normalize_then_dedupe_is_what_the_collectors_need(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolate the ordering bug the fix is about.

    The two collectors really do produce the two spellings for one PR, and the
    raw dedupe keeps both.  Normalising first collapses them to one — this is
    the contract ``plan.py`` already follows.
    """
    from agent_fleet.merge_plan.collect import dedupe_approvals
    from agent_fleet.merge_plan.plan import normalize_repo

    status = _two_spellings(tmp_path, monkeypatch)
    approvals = list(collect_from_lanes(operator=None))
    approvals += collect_from_status_dir(status, default_repo="lake-of-rage")
    assert len(approvals) == 2, "precondition: both spellings are present"
    assert {a.repo for a in approvals} == {"lake-of-rage", "Evan-Kim2028/lake-of-rage"}

    # What cmd_merge_train does today: dedupe, then filter by normalized repo.
    today = dedupe_approvals(approvals)
    today = [
        a
        for a in today
        if normalize_repo(a, {"lake-of-rage": "lake-of-rage"}).repo == "lake-of-rage"
    ]
    assert len(today) == 1, (
        f"de-dupe-before-normalize kept {len(today)} entries for one PR; this is the bug"
    )

    # What the fix must do: normalize, then dedupe (as plan.py does).
    fixed = dedupe_approvals(
        [normalize_repo(a, {"lake-of-rage": "lake-of-rage"}) for a in approvals]
    )
    assert len(fixed) == 1
