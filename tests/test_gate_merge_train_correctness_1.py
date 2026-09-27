"""A ``deploy_units``-only cluster hold must stop ``merge train``.

Claim (correctness-1): ``_held_batch`` (cli.py) matches every active hold with
``hold.matches(lane=..., deploy_unit="")`` — a hard-coded empty deploy unit.
``ClusterHold.matches`` short-circuits on a falsy ``deploy_unit`` (``return
bool(deploy_unit) and deploy_unit in self.deploy_units``), so a hold configured
with only ``match.deploy_units`` and no ``lanes`` can never match the train.  A
freeze the operator is relying on is silently bypassed and the batch lands.

This drives the real ``_held_batch`` with a real ``ExecutorSpec`` parsed from a
real fleet.yaml, over a real :class:`TrainPR` whose changed files resolve to a
deploy unit via the same ``deploy_unit_for`` the rest of merge-plan uses.  The
hold is active (never released), and the lane it would be matched on is empty —
the case ``merge run`` blocks and the train does not.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from agent_fleet.merge_plan.profile import deploy_unit_for
from agent_fleet.merge_plan.train import TrainPR

if TYPE_CHECKING:
    from pathlib import Path


def _deploy_unit_config(tmp_path: Path, units: dict[str, str]) -> Path:
    """A fleet.yaml with one active hold matched on ``deploy_units`` only."""
    config = tmp_path / "fleet.yaml"
    unit_lines = "\n".join(f"        {prefix}: {unit}" for prefix, unit in units.items())
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
        f"      path: {tmp_path / 'clone'}\n"
        "      deploy_units:\n" + unit_lines + "\n",
        encoding="utf-8",
    )
    return config


def _pr_files_deploy_unit(unit: str, units: dict[str, str]) -> tuple[tuple[str, ...], str]:
    files = (f"transform/models/{unit}_model.sql",)
    return files, deploy_unit_for(files, units)


def test_a_deploy_units_hold_stops_the_train_even_with_an_empty_lane(
    tmp_path: Path,
) -> None:
    """The claimed defect: a deploy-unit-only freeze is bypassed by the train.

    ``merge run`` matches this hold on the batch's deploy unit and refuses.  The
    train must reach the same decision for the same PR; today it hard-codes
    ``deploy_unit=\"\"`` and the hold cannot match.
    """
    from agent_fleet.merge_plan import cli as merge_cli

    units = {"transform/models/": "dbt"}
    config = _deploy_unit_config(tmp_path, units)
    files, unit = _pr_files_deploy_unit("orders", units)
    assert unit == "dbt", "precondition: the PR resolves to the frozen deploy unit"

    # A PR with no lane name — status-dir-sourced approvals have lane "".
    pr = TrainPR(number=12, head_sha="deadbeef", base_ref="main", files=files)
    args = argparse.Namespace(config=str(config))

    held = merge_cli._held_batch([pr], lanes={12: ""}, args=args)

    # The hold is active and the PR is in its frozen deploy unit, so the batch
    # must not be merged.
    assert held is not None, (
        "a deploy_units-only hold ('dbt-freeze') did not stop a PR whose deploy "
        "unit is 'dbt'; the freeze is bypassed by merge train"
    )
    assert "dbt-freeze" in held
    assert "#12" in held


def test_merge_run_and_merge_train_agree_on_a_deploy_unit_hold(tmp_path: Path) -> None:
    """The same hold, matched two ways, must reach the same answer.

    ``execute`` is the reference: it calls
    ``hold.matches(lane=p.lane, deploy_unit=batch.deploy_unit)``.  With the
    batch's real deploy unit the hold fires; with the train's hard-coded empty
    unit it does not.  The train's own docstring promises the two agree.
    """
    from agent_fleet.merge_plan import cli as merge_cli
    from agent_fleet.merge_plan.config import load_executor_spec

    units = {"transform/models/": "dbt"}
    config = _deploy_unit_config(tmp_path, units)
    files, unit = _pr_files_deploy_unit("orders", units)
    pr = TrainPR(number=12, head_sha="deadbeef", base_ref="main", files=files)

    spec = load_executor_spec(config)
    hold = spec.holds[0]
    assert hold.name == "dbt-freeze"
    assert hold.deploy_units == ("dbt",)
    assert hold.lanes == ()

    # merge run's answer: the real deploy unit is in the hold.
    assert hold.matches(lane="", deploy_unit=unit) is True, (
        "precondition: the hold must match when the deploy unit is supplied"
    )

    # The train's answer for the same hold and PR.
    args = argparse.Namespace(config=str(config))
    held = merge_cli._held_batch([pr], lanes={12: ""}, args=args)
    assert held is not None and "dbt-freeze" in held, (
        'merge train disagrees with merge run: it hard-codes deploy_unit="" so a '
        "deploy_units-only hold never matches"
    )


def test_the_hold_message_names_the_hold_and_the_prs(tmp_path: Path) -> None:
    """When the deploy-unit hold is honoured, the message must be actionable.

    This isolates the reporting contract so a fix that blocks the batch with a
    bare boolean (no message) does not pass silently.
    """
    from agent_fleet.merge_plan import cli as merge_cli

    units = {"transform/models/": "dbt"}
    config = _deploy_unit_config(tmp_path, units)
    files, _unit = _pr_files_deploy_unit("orders", units)
    prs = [
        TrainPR(number=12, head_sha="aaa", base_ref="main", files=files),
        TrainPR(number=13, head_sha="bbb", base_ref="main", files=files),
    ]
    args = argparse.Namespace(config=str(config))

    held = merge_cli._held_batch(prs, lanes={12: "", 13: ""}, args=args)
    assert held is not None, "deploy_units-only hold bypassed for two in-unit PRs"
    assert "dbt-freeze" in held
    assert "#12" in held and "#13" in held


def test_a_hold_that_matches_nothing_still_lets_the_train_through(tmp_path: Path) -> None:
    """A hold for a *different* deploy unit must not block — a control.

    Without this, a fix that simply blocks every batch would pass the tests
    above while breaking unrelated trains.
    """
    from agent_fleet.merge_plan import cli as merge_cli

    units = {"api/": "lor-api"}
    config = _deploy_unit_config(tmp_path, units)
    files = ("api/thing.py",)
    assert deploy_unit_for(files, units) == "lor-api"
    pr = TrainPR(number=12, head_sha="deadbeef", base_ref="main", files=files)
    args = argparse.Namespace(config=str(config))

    held = merge_cli._held_batch([pr], lanes={12: ""}, args=args)
    assert held is None, "a dbt-only hold must not block an api-unit PR"
