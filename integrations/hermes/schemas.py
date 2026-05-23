"""Tool schemas for cursor-fleet Hermes plugin."""

CODING_FLEET_DISPATCH = {
    "name": "coding_fleet_dispatch",
    "description": (
        "Spawn one or more Cursor SDK (Composer) coding agents with pluggable personas. "
        "Use for repo work: implement features, explore codebases, or run execute+review pipelines."
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
                "description": "Pipeline name (simple, code_review, full)",
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
