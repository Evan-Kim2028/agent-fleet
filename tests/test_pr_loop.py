"""Tests for PR loop review parsing and config."""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent_fleet.pr_loop import github_ops
from agent_fleet.pr_loop.config import load_pr_loop_config
from agent_fleet.pr_loop.review_parse import (
    find_reviewer_comment,
    has_blocking_findings,
    parse_review_risk,
)

SAMPLE_REVIEW = """\
## 🤖 Composer PR Analysis

**Risk Level:** 🟡 MEDIUM
<details open>
<summary>🟡 <b>MEDIUM</b> (2)</summary>
| # | Area | Finding |
|---|------|---------|
| 1 | ⚙️ backend | Missing test coverage |
</details>
"""


def test_has_blocking_findings_medium() -> None:
    assert has_blocking_findings(SAMPLE_REVIEW) is True


def test_has_blocking_findings_low() -> None:
    body = "**Risk Level:** 🟢 LOW\n<details><summary><b>LOW</b> (1)</summary>"
    assert has_blocking_findings(body) is False


def test_parse_review_risk_skips_agent_comments() -> None:
    comments: list[dict[str, object]] = [
        {"body": "🤖 Agent: noop"},
        {"body": SAMPLE_REVIEW},
    ]
    assert parse_review_risk(comments) == "MEDIUM"


def test_find_reviewer_comment() -> None:
    comments: list[dict[str, object]] = [{"body": SAMPLE_REVIEW}]
    assert find_reviewer_comment(comments, marker="Composer PR Analysis") == SAMPLE_REVIEW


def test_load_pr_loop_defaults() -> None:
    cfg = load_pr_loop_config(Path("/tmp"), {"pr_loop": {"enabled": True}})
    assert cfg is not None
    assert cfg.branch_prefixes == ("fleet/",)
    assert cfg.max_fix_attempts == 2
    assert cfg.ci_fix_persona is None
    assert cfg.poll_interval_s == 10
    assert cfg.review_poll_s == 10
    assert cfg.ci_poll_s == 10
    assert cfg.ci_register_poll_s == 5
    assert cfg.post_fix_poll_s == 15


def test_load_pr_loop_ci_fix_persona() -> None:
    cfg = load_pr_loop_config(
        Path("/tmp"),
        {"pr_loop": {"enabled": True, "fix_persona": "coder", "ci_fix_persona": "ci"}},
    )
    assert cfg is not None
    assert cfg.fix_persona == "coder"
    assert cfg.ci_fix_persona == "ci"


def test_files_outside_pr_scope() -> None:
    from agent_fleet.pr_loop.lifecycle import _files_outside_pr_scope

    pr_files = [
        ".github/workflows/pr-analyzer.yml",
        ".agent-fleet.yaml",
        "src/tests/test_agent_fleet_smoke.py",
    ]
    assert _files_outside_pr_scope(pr_files, [".agent-fleet.yaml"]) == ()
    assert _files_outside_pr_scope(pr_files, ["src/tests/test_new.py"]) == ()
    assert _files_outside_pr_scope(pr_files, ["README.md"]) == ("README.md",)


def test_tiered_merge_allowed_with_and_without_risk() -> None:
    from agent_fleet.pr_loop.lifecycle import tiered_merge_allowed

    blocked, reason = tiered_merge_allowed(
        ci_green=True, risk="MEDIUM", out_of_scope=[], parked=False
    )
    assert blocked is False
    assert "MEDIUM" in reason

    allowed, reason = tiered_merge_allowed(ci_green=True, risk=None, out_of_scope=[], parked=False)
    assert allowed is True
    assert reason == ""


def test_pr_loop_config_loads() -> None:
    raw = {
        "name": "sample-app",
        "pr_loop": {"enabled": True, "auto_merge": True, "fix_persona": "coder"},
    }
    cfg = load_pr_loop_config(Path("/tmp"), raw)
    assert cfg is not None
    assert cfg.enabled is True
    assert cfg.auto_merge is True


def test_prioritize_fleet_prs_newest_ready_first() -> None:
    from agent_fleet.pr_loop.watcher import prioritize_fleet_prs

    prs: list[dict[str, object]] = [
        {"number": 1601, "labels": [], "isDraft": False},
        {"number": 1625, "labels": [{"name": "fleet-ready"}], "isDraft": False},
        {"number": 1624, "labels": [{"name": "fleet-ready"}], "isDraft": True},
    ]
    state: dict[str, object] = {"pr:1601": {"parked": True}}
    ordered = prioritize_fleet_prs(prs, state, fleet_ready_label="fleet-ready")
    assert [p["number"] for p in ordered] == [1625, 1624, 1601]


def test_worktree_candidates_legacy_kimi() -> None:
    from agent_fleet.pr_loop.worktree import worktree_candidates

    base = Path("/tmp/agent-worktrees")
    cands = worktree_candidates("fleet/data/1532-0837d5d0", base)
    assert cands[0] == base / "1532-data-0837d5d0"
    assert base / "0837d5d0" in cands
    assert base / "fleet_data_1532-0837d5d0" in cands


def test_persona_from_branch_agent_prefix() -> None:
    from agent_fleet.pr_loop.lifecycle import persona_from_branch

    assert persona_from_branch("agent/backend/1499-abc12345", "backend") == "backend"
    assert persona_from_branch("fleet/data/1532-0837d5d0", "backend") == "data"


def test_persona_covering_files_and_merge_scope() -> None:
    from agent_fleet.pr_loop.lifecycle import (
        _merge_scope_out_of_scope,
        _persona_covering_files,
    )
    from agent_fleet.repo import RepoConfig

    repo = RepoConfig(
        repo_root=Path("/tmp"),
        persona_scope_allowlist={
            "coder": ("src/",),
            "infra": ("infra/", "sql/"),
            "pipe": ("pipelines/",),
        },
    )
    infra_files = ["infra/vps/deploy.sh", "infra/vps/rollback.sh"]
    assert _persona_covering_files(infra_files, repo) == "infra"
    assert _merge_scope_out_of_scope("coder", infra_files, repo) == []
    assert _merge_scope_out_of_scope("coder", ["src/a.py"], repo) == []
    assert _merge_scope_out_of_scope("coder", ["web/x.ts"], repo) == ["web/x.ts"]
    mixed = ["infra/vps/deploy.sh", "pipelines/pokemontcg_pipe/src/pipe/promote.py"]
    assert _merge_scope_out_of_scope("lakestore", mixed, repo) == []


def _init_repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """Set up a bare remote + local clone so commit_and_push can push."""
    import subprocess

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    local = tmp_path / "local"
    subprocess.run(["git", "clone", str(remote), str(local)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=local, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=local, check=True)
    (local / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=local, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=local, check=True)
    subprocess.run(
        ["git", "push", "origin", "HEAD:main"],
        cwd=local,
        check=True,
        capture_output=True,
    )
    return remote, local


def test_commit_and_push_skips_forbidden_paths(tmp_path: Path) -> None:
    """A stray .venv symlink in the worktree must not end up in the commit.

    Regression guard: silphco PR #2000 committed pipeline/.venv as a
    self-symlink because `git add -A` blindly staged everything.
    """
    from agent_fleet.pr_loop.github_ops import commit_and_push

    _, local = _init_repo_with_remote(tmp_path)
    subprocess.run(["git", "checkout", "-b", "feature-branch"], cwd=local, check=True)
    (local / "feature.txt").write_text("feature\n", encoding="utf-8")
    pipeline = local / "pipeline"
    pipeline.mkdir()
    (pipeline / ".venv").symlink_to(pipeline / ".venv")  # self-referential
    (local / "node_modules").mkdir()
    (local / "node_modules" / "junk.js").write_text("// junk\n", encoding="utf-8")

    result = commit_and_push(local, "test: forbidden filter", "feature-branch")
    assert result.ok is True
    committed = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=local,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert "feature.txt" in committed
    assert not any(".venv" in p or "node_modules" in p for p in committed), committed


def test_commit_and_push_retries_after_pre_commit_autofix(tmp_path: Path) -> None:
    """Pre-commit hook auto-rewrites a file and exits 1 on first call;
    commit_and_push should re-stage and succeed instead of returning False."""
    from agent_fleet.pr_loop.github_ops import commit_and_push

    _, local = _init_repo_with_remote(tmp_path)
    counter = tmp_path / "hook_calls"
    hook = local / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/usr/bin/env bash\n"
        f"n=$(cat {counter} 2>/dev/null || echo 0)\n"
        f"echo $((n+1)) > {counter}\n"
        'if [ "$n" -eq 0 ]; then\n'
        "  printf 'autofix\\n' > autofixed.txt\n"
        "  exit 1\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    subprocess.run(["git", "checkout", "-b", "feature-branch"], cwd=local, check=True)
    (local / "feature.txt").write_text("feature\n", encoding="utf-8")

    result = commit_and_push(local, "test: autofix retry", "feature-branch")
    assert result.ok is True
    assert (local / "autofixed.txt").read_text() == "autofix\n"
    assert counter.read_text().strip() == "2"


def test_commit_and_push_returns_false_when_hook_keeps_failing(tmp_path: Path) -> None:
    """Hook fails without modifying anything — retry shouldn't paper over it."""
    from agent_fleet.pr_loop.github_ops import commit_and_push

    _, local = _init_repo_with_remote(tmp_path)
    hook = local / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/usr/bin/env bash\necho fail >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    (local / "feature.txt").write_text("feature\n", encoding="utf-8")

    result = commit_and_push(local, "test: real fail", "feature-branch")
    assert not result
    assert result.phase == "commit_failed"


def test_commit_preflight_blocks_before_commit(tmp_path: Path) -> None:
    from agent_fleet.pr_loop.github_ops import commit_and_push

    _, local = _init_repo_with_remote(tmp_path)
    subprocess.run(["git", "checkout", "-b", "feature-branch"], cwd=local, check=True)
    (local / "bad.txt").write_text("bad\n", encoding="utf-8")

    result = commit_and_push(
        local,
        "test: preflight fail",
        "feature-branch",
        preflight_commands=["false"],
    )
    assert not result.ok
    assert result.phase == "preflight_failed"


def test_maybe_unpark_pr_entry_on_new_commit() -> None:
    from agent_fleet.pr_loop.watcher import maybe_unpark_pr_entry

    entry: dict[str, object] = {
        "parked": True,
        "last_head_oid": "abc123",
        "review_addressed": True,
    }
    updated = maybe_unpark_pr_entry(entry, head_ref_oid="def456")
    assert updated.get("parked") is False
    assert "review_addressed" not in updated
    assert updated.get("last_head_oid") == "def456"


def test_maybe_unpark_pr_entry_keeps_parked_when_oid_unchanged() -> None:
    from agent_fleet.pr_loop.watcher import maybe_unpark_pr_entry

    entry: dict[str, object] = {"parked": True, "last_head_oid": "abc123"}
    updated = maybe_unpark_pr_entry(entry, head_ref_oid="abc123")
    assert updated.get("parked") is True


def test_poll_once_parks_after_max_ci_timeout_attempts(tmp_path: Path) -> None:
    """PrLoopWatcher marks a PR parked=True after max_ci_timeout_attempts ci_timeout outcomes."""
    from unittest.mock import MagicMock, patch

    from agent_fleet.pr_loop.config import PrLoopConfig
    from agent_fleet.pr_loop.lifecycle import LifecycleResult
    from agent_fleet.pr_loop.watcher import PrLoopWatcher
    from agent_fleet.repo import RepoConfig

    repo = RepoConfig(repo_root=tmp_path, default_branch="main")
    loop_config = PrLoopConfig(
        enabled=True,
        max_ci_timeout_attempts=3,
    )
    watcher = PrLoopWatcher(repo, loop_config, fleet_config=MagicMock())

    fake_pr: dict[str, object] = {
        "number": 99,
        "headRefName": "fleet/coder/99-abc12345",
        "labels": [],
        "isDraft": False,
    }
    ci_timeout_result = LifecycleResult(
        status="ci_timeout", detail="CI did not pass within timeout"
    )

    with (
        patch(
            "agent_fleet.pr_loop.watcher.github_ops.list_open_fleet_prs",
            return_value=[fake_pr],
        ),
        patch("agent_fleet.pr_loop.watcher.github_ops.pr_has_label", return_value=False),
        # One failed check with fix_attempts < max forces lifecycle to run.
        patch(
            "agent_fleet.pr_loop.watcher.github_ops.pr_checks",
            return_value=github_ops.PrChecksSnapshot(
                all_filtered=[{"name": "ci"}],
                pending=[],
                failed=[{"name": "ci"}],
                ignored_failed=[],
            ),
        ),
        patch("agent_fleet.pr_loop.watcher.github_ops.pr_comments", return_value=[]),
        patch(
            "agent_fleet.pr_loop.watcher.run_pr_lifecycle",
            return_value=ci_timeout_result,
        ) as mock_lifecycle,
    ):
        # Poll max_ci_timeout_attempts times; PR should not be parked until the last one.
        for i in range(loop_config.max_ci_timeout_attempts - 1):
            results = watcher.poll_once()
            assert results[0]["status"] == "ci_timeout"
            # Reload state to confirm not yet parked.
            from agent_fleet.state import get_pr_state, load_state

            st = load_state(watcher.state_file)
            entry = get_pr_state(st, 99)
            assert not entry.get("parked"), f"should not be parked after {i + 1} poll(s)"

        # Final poll — should now be parked.
        results = watcher.poll_once()
        assert results[0]["status"] == "ci_timeout"
        from agent_fleet.state import get_pr_state, load_state

        st = load_state(watcher.state_file)
        entry = get_pr_state(st, 99)
        assert entry.get("parked") is True, "should be parked after max_ci_timeout_attempts polls"
        assert str(loop_config.max_ci_timeout_attempts) in str(entry.get("last_detail", ""))

    assert mock_lifecycle.call_count == loop_config.max_ci_timeout_attempts


def test_poll_once_parks_on_blocked_outcome(tmp_path: Path) -> None:
    """A `blocked` lifecycle outcome must set parked=True so the watcher stops re-entering."""
    from unittest.mock import MagicMock, patch

    from agent_fleet.pr_loop.config import PrLoopConfig
    from agent_fleet.pr_loop.lifecycle import LifecycleResult
    from agent_fleet.pr_loop.watcher import PrLoopWatcher
    from agent_fleet.repo import RepoConfig
    from agent_fleet.state import get_pr_state, load_state

    repo = RepoConfig(repo_root=tmp_path, default_branch="main")
    loop_config = PrLoopConfig(enabled=True)
    watcher = PrLoopWatcher(repo, loop_config, fleet_config=MagicMock())

    fake_pr: dict[str, object] = {
        "number": 99,
        "headRefName": "fleet/coder/99-abc12345",
        "labels": [],
        "isDraft": False,
    }
    blocked_result = LifecycleResult(status="blocked", detail="out-of-scope files: foo.py")

    with (
        patch(
            "agent_fleet.pr_loop.watcher.github_ops.list_open_fleet_prs",
            return_value=[fake_pr],
        ),
        patch("agent_fleet.pr_loop.watcher.github_ops.pr_has_label", return_value=False),
        patch(
            "agent_fleet.pr_loop.watcher.github_ops.pr_checks",
            return_value=github_ops.PrChecksSnapshot(
                all_filtered=[{"name": "ci"}],
                pending=[],
                failed=[{"name": "ci"}],
                ignored_failed=[],
            ),
        ),
        patch("agent_fleet.pr_loop.watcher.github_ops.pr_comments", return_value=[]),
        patch(
            "agent_fleet.pr_loop.watcher.run_pr_lifecycle",
            return_value=blocked_result,
        ) as mock_lifecycle,
    ):
        results = watcher.poll_once()
        assert results[0]["status"] == "blocked"
        entry = get_pr_state(load_state(watcher.state_file), 99)
        assert entry.get("parked") is True, "blocked outcome must park the PR"

        # Second poll: PR is parked, lifecycle must not run again.
        results2 = watcher.poll_once()
        assert mock_lifecycle.call_count == 1, "parked PR must not re-enter lifecycle"
        assert results2 == [] or all(r.get("pr") != "99" for r in results2)


def test_commit_preflight_missing_pre_commit_binary(tmp_path: Path, monkeypatch) -> None:
    """When .pre-commit-config.yaml exists but pre-commit is absent, preflight
    must fail with an actionable install message (not FileNotFoundError)."""
    from agent_fleet.pr_loop.github_ops import run_commit_preflight

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(
        "agent_fleet.tool_env.ensure_pre_commit",
        lambda install=True: None,
    )
    ok, detail = run_commit_preflight(repo, ["a.txt"], commands=[])
    assert ok is False
    assert "pre-commit binary not found" in detail
    assert "uv tool install pre-commit" in detail


def test_commit_preflight_uses_resolved_pre_commit_path(tmp_path: Path, monkeypatch) -> None:
    """run_commit_preflight must invoke the absolute path from ensure_pre_commit."""
    from agent_fleet.pr_loop import github_ops

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    fake_bin = str(tmp_path / "fake-pre-commit")
    seen: list[list[str]] = []

    def fake_git_run(args, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(list(args))
        from subprocess import CompletedProcess

        return CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("agent_fleet.tool_env.ensure_pre_commit", lambda install=True: fake_bin)
    monkeypatch.setattr(github_ops, "_git_run", fake_git_run)
    ok, detail = github_ops.run_commit_preflight(repo, ["a.txt"], commands=[])
    assert ok is True
    assert detail == ""
    assert seen and seen[0][0] == fake_bin
    assert "run" in seen[0] and "--files" in seen[0]
