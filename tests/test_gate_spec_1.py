"""Gate check: dbt downstream expansion must use the real manifest semantics.

In a real dbt ``manifest.json``, ``parent_map`` maps each node to its **upstream**
parents (``parent_map[child] == [parents]``); the inverse relation is
``child_map``.  ``expand_downstream`` walks ``parent_map`` as though each entry
listed a node's dependents, so a change to a leaf model never picks up the
models that read it.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_fleet.merge_plan.config import builtin_spec
from agent_fleet.merge_plan.plan import render_plan_text
from agent_fleet.merge_plan.profile import (
    build_profile,
    expand_downstream,
    load_manifest_parent_map,
)
from agent_fleet.merge_plan.types import ApprovedPR, MergePlan
from agent_fleet.merge_plan.batching import plan_batches

#: A real dbt manifest fragment.  cardindex is the source; sales and report read
#: it, so editing cardindex invalidates both, transitively.
REAL_MANIFEST: dict[str, dict[str, list[str]]] = {
    "parent_map": {
        "model.transform.gold.cardindex": [],
        "model.transform.gold.sales": ["model.transform.gold.cardindex"],
        "model.transform.gold.report": ["model.transform.gold.sales"],
    },
    "child_map": {
        "model.transform.gold.cardindex": [
            "model.transform.gold.sales",
        ],
        "model.transform.gold.sales": ["model.transform.gold.report"],
        "model.transform.gold.report": [],
    },
}

EXPECTED_CLOSURE = ("gold.cardindex", "gold.sales", "gold.report")


def _write_manifest(repo: Path) -> Path:
    path = repo / "transform" / "target" / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(REAL_MANIFEST), encoding="utf-8")
    return path


def test_expand_downstream_uses_real_manifest_parent_map(tmp_path: Path) -> None:
    """expansion must follow dependents, not the upstream ``parent_map``."""
    parent_map = load_manifest_parent_map(_write_manifest(tmp_path))

    # The manifest was read, so this is not a silent empty-map fallback.
    assert parent_map, "manifest parent_map failed to load"

    assert expand_downstream(("gold.cardindex",), parent_map) == EXPECTED_CLOSURE


def test_profile_dbt_select_covers_downstream_of_edited_model(tmp_path: Path) -> None:
    """A PR editing only cardindex must still select sales and report."""
    parent_map = load_manifest_parent_map(_write_manifest(tmp_path))
    pr = ApprovedPR(repo="lake-of-rage", pr_number=12, approved_sha="abc000012", head_sha="abc000012")

    profile = build_profile(
        pr,
        files=["transform/models/gold/cardindex.sql"],
        repo_spec=builtin_spec("lake-of-rage"),
        parent_map=parent_map,
    )

    assert profile.dbt_models == ("gold.cardindex",)
    assert profile.dbt_select_source == "manifest"
    assert profile.dbt_select == EXPECTED_CLOSURE


def test_plan_renders_select_covering_downstream_dependents(tmp_path: Path) -> None:
    """The operator-facing plan text must rebuild the real dependents."""
    parent_map = load_manifest_parent_map(_write_manifest(tmp_path))
    pr = ApprovedPR(repo="lake-of-rage", pr_number=12, approved_sha="abc000012", head_sha="abc000012")

    profile = build_profile(
        pr,
        files=["transform/models/gold/cardindex.sql"],
        repo_spec=builtin_spec("lake-of-rage"),
        parent_map=parent_map,
    )
    spec = builtin_spec("lake-of-rage")
    batches = plan_batches(
        [pr],
        {(pr.repo, pr.pr_number): profile},
        repo_specs={pr.repo: spec},
        check_merges=False,
    )

    text = render_plan_text(MergePlan(batches=tuple(batches)))

    assert "dbt --select" in text
    select_line = next(line for line in text.splitlines() if "dbt --select" in line)
    assert "gold.sales" in select_line, select_line
    assert "gold.report" in select_line, select_line
