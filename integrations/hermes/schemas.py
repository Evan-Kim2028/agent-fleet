"""Tool schemas for cursor-fleet Hermes plugin."""

CODING_FLEET_DISPATCH = {
    "name": "coding_fleet_dispatch",
    "description": (
        "Dispatch one or more Agent Fleet coding personas against a repo workspace. "
        "Use for implement, explore, execute+review, or pr_review analyze pipelines."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "goal": {"type": "string", "description": "Single-task goal for the coding agent"},
            "context": {
                "type": "string",
                "description": "Extra context (file paths, errors, constraints)",
            },
            "persona": {"type": "string", "description": "Persona id from fleet.yaml"},
            "workspace": {"type": "string", "description": "Absolute path to repo/workspace"},
            "pipeline": {
                "type": "string",
                "description": "Pipeline name (simple, code_review, pr_review, full)",
            },
            "tasks": {
                "type": "array",
                "description": "Batch mode: parallel tasks",
                "items": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string"},
                        "context": {"type": "string"},
                        "persona": {"type": "string"},
                        "workspace": {"type": "string"},
                        "pipeline": {"type": "string"},
                    },
                    "required": ["goal"],
                },
            },
        },
    },
}

CODING_FLEET_LIST_PERSONAS = {
    "name": "coding_fleet_list_personas",
    "description": "List available coding fleet personas and pipelines from fleet.yaml",
    "parameters": {"type": "object", "properties": {}},
}

CODING_FLEET_SCOPE = {
    "name": "coding_fleet_scope",
    "description": (
        "Rank fleet-dispatchable tasks for a repo using thermo-nuclear code "
        "quality review standards. Returns JSON with ranked goals for dispatch."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workspace": {
                "type": "string",
                "description": "Absolute path to git repo with .agent-fleet.yaml",
            },
            "github_repo": {
                "type": "string",
                "description": "Optional owner/repo for gh issue lookup",
            },
            "issue_limit": {
                "type": "integer",
                "description": "Max open issues to include (default 20)",
            },
        },
        "required": ["workspace"],
    },
}

CODING_FLEET_SCOUT = {
    "name": "coding_fleet_scout",
    "description": (
        "Run Fleet Scouts (read-only): product discovery + technical repo map, "
        "returning a ScoutBrief with recommended_next_moves for dispatch."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workspace": {
                "type": "string",
                "description": "Absolute path to git repo with .agent-fleet.yaml",
            },
            "github_repo": {
                "type": "string",
                "description": "Optional owner/repo for gh issue lookup",
            },
            "issue_limit": {
                "type": "integer",
                "description": "Max open issues for product scout (default 20)",
            },
            "product_context": {
                "type": "string",
                "description": "Extra product/business context for the product scout",
            },
            "depth": {
                "type": "string",
                "description": "light (default) or deep",
            },
        },
        "required": ["workspace"],
    },
}

CODING_FLEET_PR_LOOP = {
    "name": "coding_fleet_pr_loop",
    "description": (
        "Run the Agent Fleet PR loop: address Composer PR review findings, "
        "wait for CI green, and auto-merge fleet/* PRs when allowed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workspace": {
                "type": "string",
                "description": (
                    "Absolute path to git repo with pr_loop.enabled in .agent-fleet.yaml"
                ),
            },
            "mode": {
                "type": "string",
                "description": "once (poll all open fleet PRs) or pr (single PR lifecycle)",
            },
            "pr_number": {
                "type": "integer",
                "description": "PR number (required when mode=pr)",
            },
            "branch": {
                "type": "string",
                "description": "Head branch (optional; resolved via gh when mode=pr)",
            },
            "skip_review_wait": {
                "type": "boolean",
                "description": "Skip waiting for review comment when mode=pr (default true)",
            },
        },
        "required": ["workspace"],
    },
}

CODING_FLEET_PR_REVIEW = {
    "name": "coding_fleet_pr_review",
    "description": (
        "Run the repo-tuned two-pass PR analyzer (Composer 2.5 by default) on a "
        "workspace diff. Uses pr_review settings from .agent-fleet.yaml when present."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workspace": {
                "type": "string",
                "description": "Absolute path to git repo with changes to review",
            },
            "base_branch": {
                "type": "string",
                "description": "Base branch for merge-base diff (default: main)",
            },
            "pr_number": {
                "type": "integer",
                "description": "Optional PR number for analyzer logs",
            },
            "output_format": {
                "type": "string",
                "description": "json (default) or comment (GitHub markdown)",
            },
        },
        "required": ["workspace"],
    },
}
