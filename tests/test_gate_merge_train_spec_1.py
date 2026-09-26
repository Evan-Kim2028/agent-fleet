"""The train must apply the *same* hold matcher ``merge run`` applies.

Claim (spec_1): ``_held_batch`` always matches cluster holds with
``deploy_unit=\"\"``, so a hold configured only with ``deploy_units`` (no
``lanes``) never blocks a train even though it blocks ``merge run``.  This
contradicts the documented promise — in ``cmd_merge_train``'s own docstring
("a freeze declared for merge run is a freeze for the train too") and in
``_held_batch``'s ("the same active holds, the same per-PR
``ClusterHold.matches``") — that a freeze is a freeze whichever command the
operator reaches for.

This test reads the *specification* as written and checks the two paths against
it: it builds the identical hold and the identical PR, feeds the batch's real
deploy unit to ``execute``'s reference matcher and the train's call to
``_held_batch``, and requires the same verdict.  A freeze declared only over
``deploy_units`` must stop the train.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from agent_fleet.merge_plan.config import load_executor_spec
from agent_fleet.merge_plan.profile import deploy_unit_for
from agent_fleet.merge_plan.train import TrainPR

if TYPE_CHECKING:
    from pathlib import Path

#: The units the frozen PR resolves through, and the file that hits them.
_UNITS = {"transform/models/": "dbt"}
_PR_FILES = ("transform/models/x.sql",)


def _config(tmp_path: Path) -> Path:
    config = tmp_path / "fleet.yaml"
    config.write_text(
        "merge_plan:\n"
        "  executor:\n"
        f"    state_dir: {tmp_path / 'state'}\n"
        "    holds:\n"
        "      - name: freeze\n"
        "        match:\n"
        "          deploy_units: ['dbt']\n"
        "  repos:\n"
        "    - name: demo\n"
        f"      path: {tmp_path / 'clone'}\n"
        "      deploy_units:\n"
        "        transform/models/: dbt\n",
        encoding="utf-8",
    )
    return config


def test_a_deploy_units_freeze_is_a_freeze_for_the_train_too(tmp_path: Path) -> None:
    """The spec promise: one freeze, both commands.

    The hold is matched on ``deploy_units`` alone, as documented and as
    ``execute.py`` matches it (with ``batch.deploy_unit``).  The train hard-codes
    an empty deploy unit, so the very same hold cannot fire for the very same PR.
    """
    from agent_fleet.merge_plan import cli as merge_cli

    config = _config(tmp_path)
    spec = load_executor_spec(config)
    hold = spec.holds[0]
    pr = TrainPR(number=42, head_sha="sha42", base_ref="main", files=_PR_FILES)

    deploy_unit = deploy_unit_for(_PR_FILES, _UNITS)
    assert deploy_unit == "dbt"

    # What the specification says both commands do: match the hold per PR.
    spec_verdict = hold.matches(lane="", deploy_unit=deploy_unit)
    assert spec_verdict is True, "precondition: the declared freeze covers this PR"

    # What the train actually does.
    held = merge_cli._held_batch([pr], lanes={42: ""}, args=argparse.Namespace(config=str(config)))
    assert held is not None, (
        "the train did not honour a freeze declared over deploy_units=['dbt'] for a "
        "PR in the dbt unit; a freeze for merge run is not a freeze for the train"
    )
    assert "freeze" in held


def test_the_train_and_merge_run_apply_the_same_matcher(tmp_path: Path) -> None:
    """Require the two paths to agree, whatever the hold form.

    ``execute.py`` is the reference matcher; the train must produce the same
    answer for the same active hold and the same PR.  Today they diverge for a
    ``deploy_units``-only hold.
    """
    from agent_fleet.merge_plan import cli as merge_cli

    config = _config(tmp_path)
    spec = load_executor_spec(config)
    hold = spec.holds[0]
    pr = TrainPR(number=42, head_sha="sha42", base_ref="main", files=_PR_FILES)
    deploy_unit = deploy_unit_for(_PR_FILES, _UNITS)

    # merge run's decision, verbatim from execute.py:1052-1055.
    merge_run_holds = any(hold.matches(lane="", deploy_unit=deploy_unit) for _ in [pr])
    # The train's decision.
    train_held = (
        merge_cli._held_batch([pr], lanes={42: ""}, args=argparse.Namespace(config=str(config)))
        is not None
    )

    assert merge_run_holds == train_held, (
        "merge run holds the batch but merge train does not (or vice versa) for the "
        "same hold and PR: the train hard-codes deploy_unit='' while merge run "
        "supplies the batch's real deploy unit"
    )


def test_a_lane_declared_freeze_is_honoured_so_the_matcher_is_reachable(tmp_path: Path) -> None:
    """A reachable control: the train does honour a lane-matched freeze.

    This shows the code path is live and that the only thing missing is the
    deploy-unit half of the matcher, not the whole hold check.
    """
    from agent_fleet.merge_plan import cli as merge_cli

    config = tmp_path / "fleet.yaml"
    config.write_text(
        "merge_plan:\n"
        "  executor:\n"
        f"    state_dir: {tmp_path / 'state'}\n"
        "    holds:\n"
        "      - name: freeze\n"
        "        match:\n"
        "          lanes: ['frozen-*']\n",
        encoding="utf-8",
    )
    pr = TrainPR(number=42, head_sha="sha42", base_ref="main")
    held = merge_cli._held_batch(
        [pr], lanes={42: "frozen-lane"}, args=argparse.Namespace(config=str(config))
    )
    assert held is not None, "a lane-matched freeze must stop the train (control)"


def test_the_train_never_receives_a_deploy_unit_from_the_batch(tmp_path: Path) -> None:
    """The batch carries a deploy unit the train could have used and did not.

    ``execute`` computes ``deploy_unit`` per batch and matches on it; the train
    builds the same PR (with its changed files) and then matches on ``\"\"``.
    The unit is available and unused.
    """
    from agent_fleet.merge_plan import cli as merge_cli

    config = _config(tmp_path)
    pr = TrainPR(number=42, head_sha="sha42", base_ref="main", files=_PR_FILES)
    deploy_unit = deploy_unit_for(_PR_FILES, _UNITS)
    assert deploy_unit == "dbt", "precondition: the unit is computable from the PR's files"

    held = merge_cli._held_batch([pr], lanes={42: ""}, args=argparse.Namespace(config=str(config)))
    assert held is not None, (
        "the train had the PR's files (so the deploy unit was computable) yet the "
        "deploy_units freeze did not apply"
    )
