"""Tests for unique, per-PR gate test file names.

A verifier's test file was named from the finding id alone
(``test_gate_contract_1.py``). Two PRs gating different branches produce the
same path in the same test directory, so merging one into main makes the other
an add/add conflict — which is what forced a rebase plus a full re-gate for
every PR after it. The lane slug belongs in the name: the file is written into
someone else's repository, and uniqueness is the property that keeps it from
colliding with work in flight.
"""

from __future__ import annotations

import re
from pathlib import Path  # noqa: TC003 - built into concrete paths at runtime

from agent_fleet.contracts.gate import Finding
from agent_fleet.gate.config import load_gate_config
from agent_fleet.gate.prompts import gate_test_name, verify_prompt


def _finding(finding_id: str = "contract-1") -> Finding:
    return Finding(
        id=finding_id,
        file="agent.py",
        line=3,
        claim="VALUE is wrong",
        repro="call VALUE -> 2, want 1",
    )


# ---------------------------------------------------------------------------
# The naming rule
# ---------------------------------------------------------------------------


def test_gate_test_name_carries_the_lane_slug() -> None:
    assert gate_test_name("fb/gaterobust", "contract-1") == "test_gate_fb_gaterobust_contract_1.py"


def test_two_prs_on_different_lanes_get_different_names() -> None:
    """The bug: identical finding ids on two lanes produced one file path."""
    here = gate_test_name("fb/silphcoanalytics", "contract-1")
    there = gate_test_name("fb/lakeofRage", "contract-1")
    assert here != there


def test_gate_test_name_normalises_every_non_alphanumeric() -> None:
    """A branch like `fb/a.b/c` must not leak dots or slashes into a filename."""
    name = gate_test_name("fb/a.b/c", "j-1")
    body = name.removeprefix("test_gate_").removesuffix(".py")
    assert re.fullmatch(r"[A-Za-z0-9_]+", body), name
    assert not body.startswith("_")
    assert not body.endswith("_")


def test_gate_test_name_survives_an_empty_lane_or_id() -> None:
    """Never produce ``test_gate__.py``: the archive keys on the file name."""
    assert gate_test_name("", "") == "test_gate_x_x.py"
    assert gate_test_name("fb/lane", "").endswith("_x.py")


def test_gate_test_name_is_bounded() -> None:
    """A very long branch or finding id must not produce an unopenable path."""
    name = gate_test_name("fb/" + "x" * 300, "y" * 300)
    assert len(name) < 90
    assert name.startswith("test_gate_") and name.endswith(".py")


# ---------------------------------------------------------------------------
# The prompt asks for that name, in both places it appears
# ---------------------------------------------------------------------------


def _prompt(name: str) -> str:
    return verify_prompt(
        finding=_finding(),
        worktree="/tmp/wt",
        base_branch="origin/main",
        head_sha="abc123def",
        pr_number=7,
        test_dir_hint="tests",
        pytest_cmd_hint="(cd /tmp/wt && pytest -q tests/)",
        test_file_name=name,
    )


def test_verify_prompt_asks_for_the_unique_name() -> None:
    name = "test_gate_fb_gaterobust_contract_1.py"
    prompt = _prompt(name)
    assert f"Create exactly ONE new test file named {name}" in prompt


def test_verify_prompt_example_and_instruction_name_the_same_file() -> None:
    """Two copies of the name in one prompt that can drift is how the old
    ``test_gate_x.py`` placeholder placeholder in the pytest hint survived."""
    name = "test_gate_fb_lane_contract_1.py"
    prompt = _prompt(name)
    assert prompt.count(name) >= 2
    assert "test_gate_x.py" not in prompt


def test_verify_prompt_runs_the_exact_file_it_asked_for() -> None:
    name = "test_gate_fb_lane_contract_1.py"
    assert f"pytest -q tests/) {name}`" in _prompt(name)


# ---------------------------------------------------------------------------
# The slug is configuration, not a constant buried in the pipeline
# ---------------------------------------------------------------------------


def test_lane_slug_is_a_configurable_gate_setting() -> None:
    assert load_gate_config({"gate": {"lane_slug": "fb/lane"}}).lane_slug == "fb_lane"


def test_lane_slug_defaults_to_empty_so_the_head_ref_can_supply_it() -> None:
    assert load_gate_config({}).lane_slug == ""


def test_the_pipeline_keeps_the_slug_it_was_given(tmp_path: Path) -> None:
    """An explicit slug reaches the pipeline verbatim; the config is the fallback."""
    from agent_fleet.gate.config import GateConfig
    from agent_fleet.gate.pipeline import GatePipeline
    from agent_fleet.model_policy import ModelPolicy

    def _pipe(**kwargs: str) -> GatePipeline:
        return GatePipeline(
            repo=tmp_path / "repo",
            pr_number=1,
            config=GateConfig(**({"lane_slug": kwargs["cfg"]} if "cfg" in kwargs else {})),
            policy=ModelPolicy(backends={}),
            backend=object(),  # type: ignore[arg-type]
            gate_dir=tmp_path / "gate",
            use_systemd=False,
            **{k: v for k, v in kwargs.items() if k in {"lane_slug"}},
        )

    assert _pipe(lane_slug="fb/gaterobust").lane_slug == "fb/gaterobust"
    assert _pipe(cfg="fb_from_config").lane_slug == "fb_from_config"
    assert _pipe().lane_slug == ""
