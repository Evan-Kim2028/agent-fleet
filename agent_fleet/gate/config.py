"""Gate pipeline configuration from .agent-fleet.yaml.

The gate is configured per-repo under a ``gate:`` section, with the machine-wide
model policy coming from the global ``fleet.yaml`` (``model_policy:``) so every
lane on the box shares one approved-model list.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from agent_fleet.gate.prompts import lane_slug_token
from agent_fleet.gate.pytest_runner import DEFAULT_CACHE_DIR

logger = logging.getLogger(__name__)

# Each lens is one reviewer focus. The four defaults mirror the lanes the
# reference gate ran: correctness, contract, production safety, and spec
# conformance. A repo can add or replace lenses via ``gate.lenses``.
DEFAULT_LENSES: dict[str, str] = {
    "correctness": (
        "Logic errors, wrong results, broken edge cases, error handling, "
        "concurrency/race bugs, regressions of existing behaviour."
    ),
    "contract": (
        "Public API/MCP/schema contract breaks, response shape, params, "
        "docs/OpenAPI drift vs behaviour, backwards compatibility, missing "
        "tests for promised behaviour."
    ),
    "prodsafety": (
        "Production data safety and ops: data loss, destructive writes, "
        "unbounded memory/CPU on the VPS, missing locks/atomicity, unsafe "
        "migrations, performance regressions on hot paths, secrets exposure."
    ),
    "spec": (
        "Conformance to the task specification below: required items missing "
        "or implemented differently from what was asked."
    ),
}

DEFAULT_LENS_ORDER: tuple[str, ...] = ("correctness", "contract", "prodsafety", "spec")

#: Non-test changed lines above which a PR gets the full lens set rather than one
#: all-focus reviewer. Tests, fixtures, docs, snapshots and JSON inflate a diff
#: without adding review risk, so the count excludes them (see
#: :func:`agent_fleet.gate.gitops.diff_line_stats`).
DEFAULT_BIG_LINES = 1200

#: Path patterns that make a PR production-sensitive whatever its size. These
#: are the changes where one reviewer covering four focuses is thinner than the
#: work deserves, so they keep the parallel lenses. Configurable via
#: ``gate.prodsensitive_paths``; the list is matched with ``re.search``.
DEFAULT_PRODSENSITIVE_PATHS: tuple[str, ...] = (
    r"^infra/vps/",
    r"^\.github/workflows/",
    r"^scripts/(deploy|lor-api-ship|platform-deploy)",
    r"(^|/)migrations?/",
    r"(gold|sales).*(write|publish|restat|backfill|apply)",
    r"run_prod",
)

#: Path patterns that force a PR onto the full evidence gate. A diff that
#: touches none of them is reviewed under the lighter STANDARD bar (one
#: all-focus reviewer, no per-claim verifier, no judge) — see
#: :mod:`agent_fleet.gate.standard`. This is deliberately coarser than
#: ``prodsensitive_paths``: that list asks "does this need more than one
#: reviewer", this one asks "is the cheap bar safe at all".
#:
#: The defaults name the changes where a wrong verdict costs a sale, an identity
#: record, a schema, or a production host: gold/sales tables, identity, stamps,
#: migrations, schema, CI workflows, the VPS and deploy scripts. Configurable via
#: ``gate.sensitive_paths``; the list is matched with ``re.search`` against the
#: repo-relative path, so ``migrations?/`` catches a top-level ``migrations/``
#: and a nested one alike.
DEFAULT_SENSITIVE_PATHS: tuple[str, ...] = (
    r"gold|sales",
    r"identity",
    r"stamp",
    r"migrations?/",
    r"schema",
    r"\.github/workflows/",
    r"infra/vps/",
    r"deploy",
    r"run_prod",
)

_STD_MAX_PASSES = 3

_SCALARS: tuple[str, ...] = (
    "max_findings",
    "max_candidates",
    "lens_timeout_s",
    "verify_timeout_s",
    "judge_timeout_s",
    "fix_timeout_s",
    "test_timeout_s",
    "max_parallel_lenses",
    "max_parallel_verifiers",
    "max_fix_rounds",
    "big_lines",
    "agent_slots",
    "test_slots",
    "test_cache_ttl_s",
    "standard_max_passes",
)

#: Stages the legacy ``agent_timeout_s`` used to drive, in the order the fixer
#: needed it most. The judge is absent because that key never applied to it.
_LEGACY_TIMEOUT_KEY = "agent_timeout_s"
_LEGACY_TIMEOUT_TARGETS: tuple[str, ...] = ("lens_timeout_s", "verify_timeout_s", "fix_timeout_s")

_STRINGS: tuple[str, ...] = (
    "backend",
    "judge_backend",
    "base_branch",
    "test_memory",
    "test_cache_dir",
)

_OPTIONAL_STRINGS: tuple[str, ...] = ("model", "judge_model", "push_branch", "package_dir")

#: Normalised lane slug, used to make gate test file names unique per PR. Empty
#: by default so the PR's head ref can supply it at run time.
_OPTIONAL_SLUGS: tuple[str, ...] = ("lane_slug",)

_BOOLS: tuple[str, ...] = ("enable_fix", "enable_judge", "enable_test_cache", "tier0")

#: Pipeline role -> the budget field that governs it. Spelled out rather than
#: derived as ``f"{role}_timeout_s"`` because the two vocabularies differ: the
#: verifier role is ``verifier`` and its budget is ``verify_timeout_s``.
_STAGE_TIMEOUT_FIELDS: dict[str, str] = {
    "lens": "lens_timeout_s",
    "verifier": "verify_timeout_s",
    "verify": "verify_timeout_s",
    "judge": "judge_timeout_s",
    "fix": "fix_timeout_s",
}


@dataclass(frozen=True)
class GateConfig:
    """Settings for the ``agent-fleet gate`` PR pipeline."""

    lenses: tuple[str, ...] = DEFAULT_LENS_ORDER
    lens_focus: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_LENSES))
    backend: str = "cmd"
    model: str | None = None
    judge_backend: str = "cmd"  # owner policy: judge on cmd/space-bunny; grok is opt-in only
    judge_model: str | None = None
    max_findings: int = 12
    max_candidates: int = 12
    base_branch: str = "main"
    push_branch: str | None = None
    enable_fix: bool = True
    enable_judge: bool = True
    #: Per-stage agent budgets. A single shared number made the fix budget
    #: unusable (commit + push + a test suite never fits in a review's budget)
    #: while letting a doomed review burn a full stage of wall clock. 40 min for
    #: the reviewing stages, 90 for the one that writes and tests code.
    lens_timeout_s: int = 2400
    verify_timeout_s: int = 2400
    judge_timeout_s: int = 2400
    fix_timeout_s: int = 5400
    test_timeout_s: int = 900
    max_parallel_lenses: int = 8
    max_parallel_verifiers: int = 6
    test_memory: str = "6G"
    package_dir: str | None = None
    #: Lane slug folded into every verifier-created test file name. Empty means
    #: "use the PR's head ref", which is what makes the name unique per PR.
    lane_slug: str = ""
    # Safety net, not the stopping rule. The fix loop continues while the
    # failing set strictly shrinks; ``max_fix_rounds`` only bounds a run that
    # keeps making microscopic progress. See docs/GATE.md.
    max_fix_rounds: int = 4
    agent_slots: int = 24
    test_slots: int = 4
    #: Replay a pytest result when the worktree's full git tree (including
    #: uncommitted and untracked files), its gitignored-file digest, and the
    #: test list are unchanged, instead of paying for the run again. The gate
    #: re-runs the same set many times per merge: once per verified claim, then
    #: every fix round. Set ``enable_test_cache: false`` to rule out any replay.
    #: The key holds no absolute path, so the gate's own worktrees share an
    #: entry when they carry the same tree; a real change to any file, ignored
    #: or not, always misses. See docs/GATE.md.
    enable_test_cache: bool = True
    test_cache_dir: str = str(DEFAULT_CACHE_DIR)
    test_cache_ttl_s: int = 24 * 3600
    #: Approve a docs/tests-only PR on deterministic evidence alone (its own
    #: changed tests green at head plus the merged-tree check), skipping the
    #: model review entirely. See docs/GATE.md.
    tier0: bool = True
    #: Non-test changed lines above which review gets the full lens set.
    big_lines: int = DEFAULT_BIG_LINES
    #: Regexes for changed paths that are production-sensitive regardless of size.
    prodsensitive_paths: tuple[str, ...] = DEFAULT_PRODSENSITIVE_PATHS
    #: Regexes for changed paths that force the full evidence gate. A PR touching
    #: none of them is reviewed under the STANDARD bar (one all-focus reviewer,
    #: pass-counted fixer); see :mod:`agent_fleet.gate.standard`.
    sensitive_paths: tuple[str, ...] = DEFAULT_SENSITIVE_PATHS
    #: Consecutive STANDARD fixer passes before the bar falls back to the full
    #: evidence gate. Zero findings and green tests approve without a pass.
    standard_max_passes: int = _STD_MAX_PASSES

    def is_prodsensitive(self, path: str) -> bool:
        """Whether *path* matches a configured production-sensitive pattern."""
        return any(re.search(pattern, path) for pattern in self.prodsensitive_paths)

    def is_sensitive(self, path: str) -> bool:
        """Whether *path* matches a configured sensitive pattern.

        A match here is a veto: the PR keeps the full evidence gate whatever its
        size, because a wrong verdict on a gold/sales/identity/schema/deploy
        change is not recoverable by a re-gate pass.
        """
        return any(re.search(pattern, path) for pattern in self.sensitive_paths)

    def sensitive_paths_in(self, paths: list[str]) -> list[str]:
        """The subset of *paths* that is sensitive, in the order given."""
        return [path for path in paths if self.is_sensitive(path)]

    def focus_for(self, lens: str) -> str:
        """Reviewer focus text for *lens* (custom or default)."""
        return self.lens_focus.get(lens) or DEFAULT_LENSES.get(lens) or lens

    def stage_timeout(self, role: str) -> int:
        """The agent budget for *role*, in the pipeline's own vocabulary.

        The mapping is explicit rather than derived from the role name, because
        the two vocabularies are not the same: the pipeline's verifier role is
        ``verifier`` while its budget is ``verify_timeout_s``. Deriving the
        field as ``f"{role}_timeout_s"`` made ``verify_timeout_s`` dead config —
        parsed, documented, set in every fleet.yaml, and never applied, so every
        verifier silently ran on the reviewing budget instead.

        An unmapped role gets the reviewing budget rather than raising: too
        short a stage for a role added later is recoverable, a crash mid-run is
        not.
        """
        return int(getattr(self, _STAGE_TIMEOUT_FIELDS.get(role, "lens_timeout_s")))


def _apply_legacy_timeout(section: dict[str, Any], kwargs: dict[str, Any]) -> None:
    """Honour the pre-per-stage ``agent_timeout_s`` without letting it go stale.

    A fleet.yaml written before per-stage budgets sets one number for every
    stage that was not the judge. Dropping the key silently would hand those
    repos the new defaults and quietly change how long their runs take, so the
    value is still applied — to the stages it actually drove — and a warning
    names the replacement. An explicit per-stage key always wins, so the
    deprecation can be resolved one stage at a time.
    """
    if _LEGACY_TIMEOUT_KEY not in section:
        return
    legacy = int(section[_LEGACY_TIMEOUT_KEY])
    applied = [key for key in _LEGACY_TIMEOUT_TARGETS if key not in section and legacy > 0]
    for key in applied:
        kwargs[key] = legacy
    logger.warning(
        "gate: %s is deprecated; set %s instead (applied to: %s)",
        _LEGACY_TIMEOUT_KEY,
        " / ".join(_LEGACY_TIMEOUT_TARGETS),
        ", ".join(applied) or "nothing (per-stage keys already set)",
    )


def load_gate_config(raw: dict[str, Any] | None) -> GateConfig | None:
    """Load the ``gate:`` section; return ``None`` when the gate is disabled.

    ``gate: false`` disables it explicitly, matching the ``code_review: false``
    convention. Any other absent/malformed section yields the defaults, so a CLI
    ``gate`` run works in a repo with no config at all.
    """
    section = (raw or {}).get("gate")
    if section is False:
        return None
    defaults = GateConfig()
    if not section or not isinstance(section, dict):
        return defaults

    focus = dict(defaults.lens_focus)
    lenses = defaults.lenses
    lenses_raw = section.get("lenses")
    if isinstance(lenses_raw, dict) and lenses_raw:
        # Mapping form: custom lens name -> focus text replaces the defaults.
        focus = {str(k): str(v) for k, v in lenses_raw.items()}
        lenses = tuple(focus)
    elif isinstance(lenses_raw, list) and lenses_raw:
        lenses = tuple(str(name) for name in lenses_raw)
    extra_focus = section.get("lens_focus")
    if isinstance(extra_focus, dict):
        focus.update({str(k): str(v) for k, v in extra_focus.items()})

    kwargs: dict[str, Any] = {
        "lenses": lenses,
        "lens_focus": focus,
        "enable_fix": bool(section.get("enable_fix", defaults.enable_fix)),
        "enable_judge": bool(section.get("enable_judge", defaults.enable_judge)),
    }
    for key in _SCALARS:
        kwargs[key] = int(section.get(key, getattr(defaults, key)))
    _apply_legacy_timeout(section, kwargs)
    for key in _STRINGS:
        kwargs[key] = str(section.get(key) or getattr(defaults, key))
    for key in _OPTIONAL_STRINGS:
        value = section.get(key)
        kwargs[key] = str(value) if value else getattr(defaults, key)
    for key in _OPTIONAL_SLUGS:
        value = section.get(key)
        # A configured slug is folded on the way in so ``fb/lane`` and
        # ``fb_lane`` cannot produce two different test file names, and folded
        # through the naming rule so a long slug keeps the part of itself that
        # makes it a different lane.
        kwargs[key] = lane_slug_token(str(value)) if value else getattr(defaults, key)
    for key in _BOOLS:
        kwargs[key] = bool(section.get(key, getattr(defaults, key)))
    # An empty list is a deliberate "nothing is production-sensitive here", so
    # it is honoured rather than treated as absent: a repo that has moved its
    # deploy scripts can say so instead of editing the defaults.
    patterns = section.get("prodsensitive_paths")
    if isinstance(patterns, list):
        kwargs["prodsensitive_paths"] = tuple(str(p) for p in patterns)
    # Same rule for the sensitive list, with the same reason: "this repo has no
    # gold tables, so nothing is sensitive here" is a real answer, and silently
    # restoring the defaults would put every PR on the full gate again. An
    # empty sensitive list is a safe direction to fail open (the cheap bar) only
    # because it was asked for explicitly.
    sensitive = section.get("sensitive_paths")
    if isinstance(sensitive, list):
        kwargs["sensitive_paths"] = tuple(str(p) for p in sensitive)
    return GateConfig(**kwargs)
