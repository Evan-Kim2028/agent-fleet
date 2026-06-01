"""Unit tests for silphco.selfimprove.guard — path allowlist/denylist."""

from __future__ import annotations


from silphco.selfimprove.guard import check, is_allowed


class TestIsAllowed:
    # ---- Allowed paths ----
    def test_backend_persona_allowed(self):
        assert is_allowed("agents/personas/backend.md") is True

    def test_frontend_persona_allowed(self):
        assert is_allowed("agents/personas/frontend.md") is True

    def test_data_persona_allowed(self):
        assert is_allowed("agents/personas/data.md") is True

    def test_pokemon_analyst_persona_allowed(self):
        assert is_allowed("agents/personas/pokemon_analyst.md") is True

    def test_security_qa_persona_allowed(self):
        assert is_allowed("agents/personas/security_qa.md") is True

    def test_pr_review_overlay_allowed(self):
        assert is_allowed("agents/pr_review_overlay.md") is True

    # ---- Denylist overrides allowlist ----
    def test_guard_py_denied_even_if_globbed(self):
        # guard.py is explicitly in the denylist
        assert is_allowed("agents/silphco/selfimprove/guard.py") is False

    def test_mine_py_denied(self):
        assert is_allowed("agents/silphco/selfimprove/mine.py") is False

    def test_gate_py_denied(self):
        assert is_allowed("agents/silphco/selfimprove/gate.py") is False

    def test_loop_py_denied(self):
        assert is_allowed("agents/silphco/selfimprove/loop.py") is False

    def test_agents_dispatch_denied(self):
        assert is_allowed("agents/agents/dispatch.py") is False

    def test_agent_fleet_yaml_denied(self):
        assert is_allowed(".agent-fleet.yaml") is False

    def test_github_workflow_denied(self):
        assert is_allowed(".github/workflows/ci.yml") is False

    def test_github_root_denied(self):
        assert is_allowed(".github/CODEOWNERS") is False

    def test_test_file_denied(self):
        assert is_allowed("agents/tests/test_dispatch.py") is False

    def test_test_wildcard_denied(self):
        assert is_allowed("pipeline/tests/test_gold_builder.py") is False

    # ---- Paths outside allowlist (not in denylist) ----
    def test_pipeline_src_not_allowed(self):
        assert is_allowed("pipeline/src/gold_builder.py") is False

    def test_frontend_src_not_allowed(self):
        assert is_allowed("frontend/src/App.tsx") is False

    def test_random_python_file_not_allowed(self):
        assert is_allowed("agents/silphco/kimi_backend.py") is False

    def test_empty_path_not_allowed(self):
        assert is_allowed("") is False

    # ---- Path traversal / absolute paths ----
    def test_path_traversal_single_denied(self):
        assert is_allowed("../agents/personas/backend.md") is False

    def test_path_traversal_middle_denied(self):
        assert is_allowed("agents/../personas/backend.md") is False

    def test_absolute_path_denied(self):
        assert is_allowed("/agents/personas/backend.md") is False

    def test_absolute_path_home_denied(self):
        assert is_allowed("/home/evan/agents/personas/backend.md") is False

    def test_backslash_absolute_denied(self):
        assert is_allowed("\\agents\\personas\\backend.md") is False

    def test_double_dot_traversal_escape_denied(self):
        # Attempt to escape via ../../
        assert is_allowed("../../etc/passwd") is False

    def test_dot_dot_in_middle_denied(self):
        # Looks like a valid subpath but contains traversal
        assert is_allowed("agents/personas/../../../etc/passwd") is False


class TestCheckReturnsReason:
    def test_allowed_returns_empty_reason(self):
        allowed, reason = check("agents/personas/backend.md")
        assert allowed is True
        assert reason == ""

    def test_traversal_returns_reason(self):
        allowed, reason = check("../agents/personas/backend.md")
        assert allowed is False
        assert "traversal" in reason.lower() or "path" in reason.lower()

    def test_denylist_returns_reason(self):
        allowed, reason = check("agents/agents/dispatch.py")
        assert allowed is False
        assert "denylist" in reason.lower()

    def test_not_in_allowlist_returns_reason(self):
        allowed, reason = check("pipeline/src/gold_builder.py")
        assert allowed is False
        assert "allowlist" in reason.lower()

    def test_self_modify_guard_denied_with_reason(self):
        allowed, reason = check("agents/silphco/selfimprove/guard.py")
        assert allowed is False
        assert reason  # must have a non-empty reason
