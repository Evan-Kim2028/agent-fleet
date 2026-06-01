"""Unit tests for agents.github — subprocess mocked throughout."""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

import agents.github as gh


def _make_result(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.stdout = stdout
    r.returncode = returncode
    return r


class TestGhIssueHasLabel:
    def test_returns_true_when_label_present(self):
        payload = json.dumps({"labels": [{"name": "bug"}, {"name": "agent-running/backend/42"}]})
        with patch("subprocess.run", return_value=_make_result(payload)):
            assert gh.gh_issue_has_label(42, "agent-running/backend/42") is True

    def test_returns_false_when_label_absent(self):
        payload = json.dumps({"labels": [{"name": "bug"}]})
        with patch("subprocess.run", return_value=_make_result(payload)):
            assert gh.gh_issue_has_label(42, "agent-running/backend/42") is False

    def test_returns_false_on_gh_error(self):
        with patch("subprocess.run", return_value=_make_result("", returncode=1)):
            assert gh.gh_issue_has_label(42, "any-label") is False


class TestGhPrChecks:
    def test_returns_parsed_list(self):
        payload = json.dumps([{"name": "ci", "state": "COMPLETED", "conclusion": "SUCCESS"}])
        with patch("subprocess.run", return_value=_make_result(payload)):
            checks = gh.gh_pr_checks(1)
        assert checks[0]["name"] == "ci"

    def test_returns_empty_on_error(self):
        with patch("subprocess.run", return_value=_make_result("", returncode=1)):
            assert gh.gh_pr_checks(1) == []

    def test_returns_empty_on_invalid_json(self):
        with patch("subprocess.run", return_value=_make_result("not-json")):
            assert gh.gh_pr_checks(1) == []


class TestGhDefaultBranch:
    def test_parses_main(self):
        payload = json.dumps({"defaultBranchRef": {"name": "main"}})
        with patch("subprocess.run", return_value=_make_result(payload)):
            assert gh.gh_default_branch() == "main"

    def test_falls_back_on_error(self):
        with patch("subprocess.run", return_value=_make_result("", returncode=1)):
            assert gh.gh_default_branch() == "main"


class TestSetRepo:
    def test_set_repo_injects_gh_repo_env(self):
        gh.set_repo("myorg/myrepo")
        captured_env = {}

        def capture_run(cmd, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            return _make_result(json.dumps({"labels": []}))

        with patch("subprocess.run", side_effect=capture_run):
            gh.gh_issue_has_label(1, "x")

        assert captured_env.get("GH_REPO") == "myorg/myrepo"
        gh.set_repo("")  # reset


class TestGitPushWithUserToken:
    """The push path must use the user PAT in the URL, scrub bot-scoped tokens
    from env, and disable the credential helper so nothing can quietly swap in
    a GitHub App installation token (which would suppress CI triggers)."""

    @pytest.fixture(autouse=True)
    def _isolated_push_lock(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gh, "_PUSH_LOCK_PATH", str(tmp_path / "agent-push.lock"))
        monkeypatch.setattr("agents.github.time.sleep", lambda *_a, **_k: None)

    def _setup(self):
        gh.set_repo("acme/widgets")
        calls: list[dict] = []

        def fake_run(cmd, **kwargs):
            calls.append({"cmd": cmd, "env": kwargs.get("env", {}), "cwd": kwargs.get("cwd")})
            if cmd[:3] == ["gh", "auth", "token"]:
                return _make_result("gho_fake_user_token\n")
            return _make_result("")

        return calls, fake_run

    def test_push_uses_pat_in_url(self):
        calls, fake_run = self._setup()
        try:
            with patch("subprocess.run", side_effect=fake_run), \
                 patch.dict("os.environ", {"GITHUB_TOKEN": "ghs_bad", "GH_TOKEN": "ghs_bad2"}, clear=False):
                gh.git_push_with_user_token(MagicMock(), "feat/x", set_upstream=True)
        finally:
            gh.set_repo("")

        push_call = next(c for c in calls if c["cmd"][0] == "git")
        cmd = push_call["cmd"]
        assert cmd[:5] == ["git", "-c", "credential.helper=", "push", "-u"]
        assert cmd[-2] == "https://x-access-token:gho_fake_user_token@github.com/acme/widgets.git"
        assert cmd[-1] == "feat/x"

    def test_push_scrubs_bot_tokens_from_env(self):
        calls, fake_run = self._setup()
        try:
            with patch("subprocess.run", side_effect=fake_run), \
                 patch.dict("os.environ", {"GITHUB_TOKEN": "ghs_bad", "GH_TOKEN": "ghs_bad2"}, clear=False):
                gh.git_push_with_user_token(MagicMock(), "feat/x")
        finally:
            gh.set_repo("")

        push_call = next(c for c in calls if c["cmd"][0] == "git")
        assert "GITHUB_TOKEN" not in push_call["env"]
        assert "GH_TOKEN" not in push_call["env"]

    def test_push_force_with_lease(self):
        calls, fake_run = self._setup()
        try:
            with patch("subprocess.run", side_effect=fake_run):
                gh.git_push_with_user_token(MagicMock(), "feat/x", force_with_lease=True)
        finally:
            gh.set_repo("")

        push_cmd = next(c["cmd"] for c in calls if c["cmd"][0] == "git")
        assert "--force-with-lease" in push_cmd
        assert "-u" not in push_cmd

    def test_push_raises_when_repo_not_set(self):
        gh.set_repo("")
        with patch("subprocess.run", return_value=_make_result("gho_fake")):
            try:
                gh.git_push_with_user_token(MagicMock(), "feat/x")
                assert False, "expected RuntimeError"
            except RuntimeError as exc:
                assert "set_repo" in str(exc)

    def test_push_raises_when_token_empty(self):
        gh.set_repo("acme/widgets")
        try:
            with patch("subprocess.run", return_value=_make_result("")):
                try:
                    gh.git_push_with_user_token(MagicMock(), "feat/x")
                    assert False, "expected RuntimeError"
                except RuntimeError as exc:
                    assert "empty" in str(exc)
        finally:
            gh.set_repo("")

    def test_push_captures_output_so_token_url_does_not_stream_to_journald(self):
        """``git push -u`` prints an upstream-tracking line containing the
        token-in-URL. We must capture stderr so it never reaches the
        parent stdio (and thus systemd-journal). Regression: 2026-05 leak."""
        calls, fake_run = self._setup()
        try:
            with patch("subprocess.run", side_effect=fake_run):
                gh.git_push_with_user_token(MagicMock(), "feat/x", set_upstream=True)
        finally:
            gh.set_repo("")
        push_call = next(c for c in calls if c["cmd"][0] == "git")
        assert push_call.get("cmd") is not None
        # The fake_run dict captures kwargs; capture_output must be True so
        # subprocess does not inherit the parent's stdio.

    def test_push_raises_with_redacted_token_on_failure(self):
        """When git push fails, the re-raised exception must NOT contain any
        gho_/ghp_/ghs_/github_pat_ substring. Otherwise CalledProcessError
        str() leaks the token into tracebacks and journald."""
        gh.set_repo("acme/widgets")

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["gh", "auth", "token"]:
                return _make_result("gho_fake_user_token_abc123\n")
            # Simulate git push failure with the leak pattern in stderr.
            r = MagicMock(spec=subprocess.CompletedProcess)
            r.returncode = 1
            r.stdout = ""
            r.stderr = (
                "remote: Permission denied\n"
                "fatal: unable to access "
                "'https://x-access-token:gho_fake_user_token_abc123@github.com/acme/widgets.git/': "
                "The requested URL returned error: 403\n"
            )
            r.args = cmd
            return r

        try:
            with patch("subprocess.run", side_effect=fake_run), \
                 pytest.raises(subprocess.CalledProcessError) as excinfo:
                gh.git_push_with_user_token(MagicMock(), "feat/x", set_upstream=True)
            err = excinfo.value
            blob = "\n".join(filter(None, [err.stdout or "", err.stderr or "", " ".join(map(str, err.cmd))]))
            assert "gho_fake_user_token_abc123" not in blob
            assert "<redacted>" in (err.stderr or "")
            assert "<redacted-remote-url>" in err.cmd
        finally:
            gh.set_repo("")


class TestRedactTokens:
    def test_redacts_gho_user_oauth(self):
        assert gh._redact_tokens("https://x-access-token:gho_abcDEF123@github.com/") == \
            "https://x-access-token:<redacted>@github.com/"

    def test_redacts_ghp_user_pat(self):
        assert "<redacted>" in gh._redact_tokens("ghp_1234567890abcdef")
        assert "ghp_1234567890abcdef" not in gh._redact_tokens("ghp_1234567890abcdef")

    def test_redacts_fine_grained_pat(self):
        redacted = gh._redact_tokens("token=github_pat_11AAAAAAAAA_abcXYZ end")
        assert "github_pat_11AAAAAAAAA_abcXYZ" not in redacted
        assert "<redacted>" in redacted

    def test_passes_through_safe_text(self):
        assert gh._redact_tokens("nothing to redact here") == "nothing to redact here"
        assert gh._redact_tokens("") == ""


class TestGhPrMerge:
    """gh_pr_merge MUST NOT pass --delete-branch — that flag has gh attempt
    a local ``git branch -d`` on the parent repo, which fails noisily when
    the branch is still checked out in the agent worktree. Remote branch
    deletion is handled by gh_delete_remote_branch after worktree teardown.
    """

    def test_merge_invocation_does_not_pass_delete_branch(self):
        gh.set_repo("acme/widgets")
        captured: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            captured.append(cmd)
            return _make_result("", returncode=0)

        try:
            with patch("subprocess.run", side_effect=fake_run):
                gh.gh_pr_merge(123, "subj", "body")
        finally:
            gh.set_repo("")

        merge_cmd = next(c for c in captured if "merge" in c)
        assert "--delete-branch" not in merge_cmd
        assert "--squash" in merge_cmd


class TestGhDeleteRemoteBranch:
    def test_returns_true_on_204_success(self):
        gh.set_repo("acme/widgets")
        try:
            with patch("subprocess.run", return_value=_make_result("", returncode=0)):
                assert gh.gh_delete_remote_branch("agent/data/1234-abcd") is True
        finally:
            gh.set_repo("")

    def test_returns_true_when_ref_already_gone(self):
        """422 "Reference does not exist" is benign — someone else deleted
        it (e.g. gh's stale --delete-branch in a prior run) and the goal
        of "remote branch is gone" is already achieved."""
        gh.set_repo("acme/widgets")
        r = MagicMock(spec=subprocess.CompletedProcess)
        r.returncode = 22  # gh maps HTTP 422 → exit 22
        r.stdout = ""
        r.stderr = '{"message":"Reference does not exist","documentation_url":"..."}'
        try:
            with patch("subprocess.run", return_value=r):
                assert gh.gh_delete_remote_branch("agent/data/1234-abcd") is True
        finally:
            gh.set_repo("")

    def test_returns_false_on_unexpected_error(self):
        gh.set_repo("acme/widgets")
        r = MagicMock(spec=subprocess.CompletedProcess)
        r.returncode = 1
        r.stdout = ""
        r.stderr = "network unreachable"
        try:
            with patch("subprocess.run", return_value=r):
                assert gh.gh_delete_remote_branch("agent/data/1234-abcd") is False
        finally:
            gh.set_repo("")

    def test_returns_false_when_branch_name_empty(self):
        gh.set_repo("acme/widgets")
        try:
            # Should NOT hit subprocess at all.
            with patch("subprocess.run") as run_mock:
                assert gh.gh_delete_remote_branch("") is False
                run_mock.assert_not_called()
        finally:
            gh.set_repo("")

    def test_returns_false_when_repo_not_set(self):
        gh.set_repo("")
        with patch("subprocess.run") as run_mock:
            assert gh.gh_delete_remote_branch("agent/data/1") is False
            run_mock.assert_not_called()


class TestWaitForCITrigger:
    def test_returns_true_when_github_actions_present(self):
        gh.set_repo("acme/widgets")
        payload = json.dumps({
            "check_suites": [
                {"app": {"slug": "claude"}},
                {"app": {"slug": "github-actions"}},
            ],
        })
        try:
            with patch("subprocess.run", return_value=_make_result(payload)), \
                 patch("time.sleep"):
                assert gh.wait_for_ci_trigger("abc123", attempts=1, interval=0) is True
        finally:
            gh.set_repo("")

    def test_returns_false_when_only_other_apps_present(self):
        gh.set_repo("acme/widgets")
        payload = json.dumps({"check_suites": [{"app": {"slug": "claude"}}]})
        try:
            with patch("subprocess.run", return_value=_make_result(payload)), \
                 patch("time.sleep"):
                assert gh.wait_for_ci_trigger("abc123", attempts=2, interval=0) is False
        finally:
            gh.set_repo("")

    def test_handles_api_error_gracefully(self):
        gh.set_repo("acme/widgets")
        try:
            with patch("subprocess.run", return_value=_make_result("", returncode=1)), \
                 patch("time.sleep"):
                assert gh.wait_for_ci_trigger("abc123", attempts=2, interval=0) is False
        finally:
            gh.set_repo("")


class TestPushStaggerLock:
    """`git_push_with_user_token` must serialize parallel pushes through a
    filesystem lock and sleep out the configured min-interval, so GitHub
    never sees two near-simultaneous pushes from the same actor (which it
    silently coalesces into a single workflow trigger)."""

    def test_first_push_does_not_sleep(self, tmp_path, monkeypatch):
        lock_path = tmp_path / "agent-push.lock"
        monkeypatch.setattr(gh, "_PUSH_LOCK_PATH", str(lock_path))
        gh.set_repo("acme/widgets")

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["gh", "auth", "token"]:
                return _make_result("gho_fake\n")
            return _make_result("")

        sleeps: list[float] = []
        try:
            with patch("subprocess.run", side_effect=fake_run), \
                 patch("agents.github.time.sleep", side_effect=sleeps.append):
                gh.git_push_with_user_token(MagicMock(), "feat/x")
        finally:
            gh.set_repo("")

        # No prior push → no wait.
        assert sleeps == []
        # Lock file now records the completion timestamp.
        assert lock_path.exists()
        assert float(lock_path.read_text().strip()) > 0

    def test_second_push_waits_for_min_interval(self, tmp_path, monkeypatch):
        import time as _time
        lock_path = tmp_path / "agent-push.lock"
        monkeypatch.setattr(gh, "_PUSH_LOCK_PATH", str(lock_path))
        # Pre-seed lock file with "previous push happened just now".
        lock_path.write_text(f"{_time.time():.3f}\n")
        gh.set_repo("acme/widgets")

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["gh", "auth", "token"]:
                return _make_result("gho_fake\n")
            return _make_result("")

        sleeps: list[float] = []
        try:
            with patch("subprocess.run", side_effect=fake_run), \
                 patch("agents.github.time.sleep", side_effect=sleeps.append):
                gh.git_push_with_user_token(MagicMock(), "feat/x")
        finally:
            gh.set_repo("")

        # Must have slept for ~MIN_INTERVAL seconds.
        assert len(sleeps) == 1
        assert sleeps[0] > 0
        assert sleeps[0] <= gh._PUSH_MIN_INTERVAL_SECONDS + 0.5

    def test_lock_released_on_push_failure(self, tmp_path, monkeypatch):
        """If the git push subprocess fails, the lock must still be released
        and the timestamp recorded — otherwise a crashed agent would block
        every subsequent push indefinitely."""
        lock_path = tmp_path / "agent-push.lock"
        monkeypatch.setattr(gh, "_PUSH_LOCK_PATH", str(lock_path))
        gh.set_repo("acme/widgets")

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["gh", "auth", "token"]:
                return _make_result("gho_fake\n")
            raise subprocess.CalledProcessError(1, cmd)

        try:
            with patch("subprocess.run", side_effect=fake_run), \
                 patch("agents.github.time.sleep"):
                try:
                    gh.git_push_with_user_token(MagicMock(), "feat/x")
                except subprocess.CalledProcessError:
                    pass
        finally:
            gh.set_repo("")

        # Slot still got marked so the next push isn't blocked forever by a
        # stale empty lock.
        assert lock_path.exists()
        assert float(lock_path.read_text().strip()) > 0


class TestGitPushWithSafeRebase:
    """git_push_with_safe_rebase must retry non-fast-forward pushes by
    fetching the remote branch and rebasing."""

    @pytest.fixture(autouse=True)
    def _isolated_push_lock(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gh, "_PUSH_LOCK_PATH", str(tmp_path / "agent-push.lock"))
        monkeypatch.setattr("agents.github.time.sleep", lambda *_a, **_k: None)

    def _setup(self):
        gh.set_repo("acme/widgets")
        calls: list[dict] = []

        def fake_run(cmd, **kwargs):
            calls.append({"cmd": cmd, "env": kwargs.get("env", {}), "cwd": kwargs.get("cwd")})
            if cmd[:3] == ["gh", "auth", "token"]:
                return _make_result("gho_fake_user_token\n")
            return _make_result("")

        return calls, fake_run

    def test_success_on_first_push(self):
        calls, fake_run = self._setup()
        try:
            with patch("subprocess.run", side_effect=fake_run):
                success, error = gh.git_push_with_safe_rebase(MagicMock(), "feat/x")
        finally:
            gh.set_repo("")
        assert success is True
        assert error is None
        push_calls = [c for c in calls if c["cmd"][0] == "git" and c["cmd"][3] == "push"]
        assert len(push_calls) == 1

    def test_rebase_and_retry_on_non_fast_forward(self):
        calls, fake_run = self._setup()

        def tracking_run(cmd, **kwargs):
            calls.append({"cmd": cmd, "env": kwargs.get("env", {}), "cwd": kwargs.get("cwd")})
            if cmd[:3] == ["gh", "auth", "token"]:
                return _make_result("gho_fake_user_token\n")
            if cmd[0] == "git" and "push" in cmd:
                push_attempts = len([c for c in calls if c["cmd"][0] == "git" and "push" in c["cmd"]])
                if push_attempts == 1:
                    raise subprocess.CalledProcessError(1, cmd, stderr="non-fast-forward")
            return _make_result("")

        try:
            with patch("subprocess.run", side_effect=tracking_run):
                success, error = gh.git_push_with_safe_rebase(MagicMock(), "feat/x")
        finally:
            gh.set_repo("")
        assert success is True
        assert error is None
        # Should have done fetch + rebase + push retry
        git_calls = [c["cmd"] for c in calls if c["cmd"][0] == "git"]
        assert any("fetch" in c for c in git_calls)
        assert any("rebase" in c for c in git_calls)
        assert sum(1 for c in git_calls if "push" in c) == 2

    def test_returns_rebase_conflict_on_content_conflict(self):
        calls, fake_run = self._setup()

        def tracking_run(cmd, **kwargs):
            calls.append({"cmd": cmd, "env": kwargs.get("env", {}), "cwd": kwargs.get("cwd")})
            if cmd[:3] == ["gh", "auth", "token"]:
                return _make_result("gho_fake_user_token\n")
            if cmd[0] == "git" and "push" in cmd:
                raise subprocess.CalledProcessError(1, cmd, stderr="non-fast-forward")
            if cmd[0] == "git" and "rebase" in cmd:
                return _make_result("CONFLICT", returncode=1)
            return _make_result("")

        try:
            with patch("subprocess.run", side_effect=tracking_run):
                success, error = gh.git_push_with_safe_rebase(MagicMock(), "feat/x")
        finally:
            gh.set_repo("")
        assert success is False
        assert error == "rebase_conflict"
        # Should have aborted the rebase
        abort_calls = [c["cmd"] for c in calls if c["cmd"][0] == "git" and "--abort" in c["cmd"]]
        assert len(abort_calls) == 1

    def test_returns_push_failed_for_other_errors(self):
        calls, fake_run = self._setup()

        def tracking_run(cmd, **kwargs):
            calls.append({"cmd": cmd, "env": kwargs.get("env", {}), "cwd": kwargs.get("cwd")})
            if cmd[:3] == ["gh", "auth", "token"]:
                return _make_result("gho_fake_user_token\n")
            if cmd[0] == "git" and "push" in cmd:
                raise subprocess.CalledProcessError(1, cmd, stderr="network error")
            return _make_result("")

        try:
            with patch("subprocess.run", side_effect=tracking_run):
                success, error = gh.git_push_with_safe_rebase(MagicMock(), "feat/x")
        finally:
            gh.set_repo("")
        assert success is False
        assert error == "push_failed"


class TestGhListOpenAgentPrs:
    """Auto-merge sweeper must see both legacy ``agent/`` and current ``fleet/`` PR branches."""

    def test_includes_agent_branch(self):
        payload = json.dumps([{"number": 1, "headRefName": "agent/backend/42-abc"}])
        with patch("subprocess.run", return_value=_make_result(payload)):
            prs = gh.gh_list_open_agent_prs()
        assert [pr["number"] for pr in prs] == [1]

    def test_includes_fleet_branch(self):
        payload = json.dumps([{"number": 2, "headRefName": "fleet/data/926-a31b729a"}])
        with patch("subprocess.run", return_value=_make_result(payload)):
            prs = gh.gh_list_open_agent_prs()
        assert [pr["number"] for pr in prs] == [2]

    def test_filters_unrelated_branches(self):
        payload = json.dumps(
            [
                {"number": 1, "headRefName": "agent/backend/42-abc"},
                {"number": 2, "headRefName": "fleet/data/926-xyz"},
                {"number": 3, "headRefName": "fix/something-else"},
                {"number": 4, "headRefName": "main"},
            ]
        )
        with patch("subprocess.run", return_value=_make_result(payload)):
            prs = gh.gh_list_open_agent_prs()
        assert sorted(pr["number"] for pr in prs) == [1, 2]

    def test_returns_empty_on_gh_error(self):
        with patch("subprocess.run", return_value=_make_result("", returncode=1)):
            assert gh.gh_list_open_agent_prs() == []

    def test_returns_empty_on_invalid_json(self):
        with patch("subprocess.run", return_value=_make_result("not-json")):
            assert gh.gh_list_open_agent_prs() == []


def _res(stdout="", returncode=0, stderr=""):
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.stdout = stdout
    r.returncode = returncode
    r.stderr = stderr
    return r


class TestMergeIntegrityFalsePositive:
    """#1157: a transient concurrent-git failure must NOT be reported as a
    merge-integrity error (the caller may auto-revert a good merge on it)."""

    def test_transient_fetch_lock_is_inconclusive_not_error(self):
        # First fetch fails with the ref-lock race; retry succeeds.
        calls = []

        def fake_run(args, **_kw):
            calls.append(args)
            if args[:2] == ["git", "fetch"]:
                if len([c for c in calls if c[:2] == ["git", "fetch"]]) == 1:
                    return _res(
                        returncode=1,
                        stderr="error: cannot lock ref "
                        "'refs/remotes/origin/main': is at abc but expected def",
                    )
                return _res(returncode=0)
            if args[:2] == ["git", "ls-tree"] and args[-1] == "data":
                return _res(stdout="040000 tree deadbeef\tdata")
            return _res(stdout="")

        with patch("subprocess.run", side_effect=fake_run), \
             patch("agents.github.gh_default_branch", return_value="main"), \
             patch("agents.github.time.sleep", lambda *_a, **_k: None):
            assert gh.gh_validate_merge_integrity() == []

    def test_persistent_fetch_lock_returns_empty_not_false_error(self):
        def fake_run(args, **_kw):
            if args[:2] == ["git", "fetch"]:
                return _res(
                    returncode=1,
                    stderr="fatal: Unable to create '.../index.lock': "
                    "File exists",
                )
            return _res(stdout="")

        with patch("subprocess.run", side_effect=fake_run), \
             patch("agents.github.gh_default_branch", return_value="main"), \
             patch("agents.github.time.sleep", lambda *_a, **_k: None):
            # Inconclusive → empty, NOT ["post-merge fetch failed: ..."].
            assert gh.gh_validate_merge_integrity() == []

    def test_genuine_missing_data_still_reported(self):
        def fake_run(args, **_kw):
            if args[:2] == ["git", "fetch"]:
                return _res(returncode=0)
            if args[:2] == ["git", "ls-tree"] and args[-1] == "data":
                return _res(stdout="", returncode=0)  # genuinely absent
            return _res(stdout="")

        with patch("subprocess.run", side_effect=fake_run), \
             patch("agents.github.gh_default_branch", return_value="main"), \
             patch("agents.github.time.sleep", lambda *_a, **_k: None):
            assert "data/ missing from tree" in gh.gh_validate_merge_integrity()


def test_gh_pr_has_label_true_and_false():
    with patch("agents.github._gh", return_value=_make_result(
        stdout='{"labels": [{"name": "needs-human-review"}, {"name": "foo"}]}',
        returncode=0,
    )):
        assert gh.gh_pr_has_label(123, "needs-human-review") is True
        assert gh.gh_pr_has_label(123, "absent") is False


def test_gh_pr_has_label_failopen_true_on_error():
    with patch("agents.github._gh", return_value=_make_result(stdout="", returncode=1)):
        # Fail-safe: cannot read labels -> assume the hold MAY be present.
        assert gh.gh_pr_has_label(123, "needs-human-review") is True


def test_gh_pr_has_label_failopen_true_on_bad_json():
    with patch("agents.github._gh", return_value=_make_result(stdout="not json", returncode=0)):
        assert gh.gh_pr_has_label(123, "needs-human-review") is True


def test_gh_pr_diff_returns_stdout():
    with patch("agents.github._gh", return_value=_make_result(
        stdout="--- a/x\n+++ b/x\n@@ -1 +0,0 @@\n-gone\n", returncode=0,
    )):
        assert "-gone" in gh.gh_pr_diff(5)


def test_gh_pr_diff_failopen_empty_on_error():
    with patch("agents.github._gh", return_value=_make_result(stdout="x", returncode=1)):
        assert gh.gh_pr_diff(5) == ""
