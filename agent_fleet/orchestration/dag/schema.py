"""DAG JSON schema — cookbook-compatible with fleet extensions."""

# ruff: noqa: TC003

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema

from agent_fleet._schema import load_schema

Complexity = str  # HIGH | MED | LOW


@dataclass(frozen=True)
class DagTask:
    id: str
    depends_on: tuple[str, ...]
    complexity: Complexity
    subtask_prompt: str
    persona: str | None = None
    pipeline: str | None = None
    allowed_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class DagSpec:
    title: str
    tasks: tuple[DagTask, ...]
    models: dict[str, str] = field(default_factory=dict)

    def task_ids(self) -> set[str]:
        return {task.id for task in self.tasks}

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "models": dict(self.models),
            "tasks": [
                {
                    "id": task.id,
                    "depends_on": list(task.depends_on),
                    "complexity": task.complexity,
                    "subtask_prompt": task.subtask_prompt,
                    **({"persona": task.persona} if task.persona else {}),
                    **({"pipeline": task.pipeline} if task.pipeline else {}),
                    **({"allowed_paths": list(task.allowed_paths)} if task.allowed_paths else {}),
                }
                for task in self.tasks
            ],
        }


def validate_dag_spec(data: dict[str, Any]) -> None:
    """Raise jsonschema.ValidationError if data does not match dag schema."""
    jsonschema.validate(instance=data, schema=load_schema("dag"))


def dag_spec_from_dict(data: dict[str, Any]) -> DagSpec:
    validate_dag_spec(data)
    tasks: list[DagTask] = []
    for raw in data["tasks"]:
        allowed = raw.get("allowed_paths") or []
        tasks.append(
            DagTask(
                id=str(raw["id"]),
                depends_on=tuple(str(dep) for dep in raw["depends_on"]),
                complexity=str(raw["complexity"]),
                subtask_prompt=str(raw["subtask_prompt"]),
                persona=str(raw["persona"]) if raw.get("persona") else None,
                pipeline=str(raw["pipeline"]) if raw.get("pipeline") else None,
                allowed_paths=tuple(str(p) for p in allowed),
            )
        )
    models_raw = data.get("models") or {}
    models = {str(k): str(v) for k, v in models_raw.items()} if isinstance(models_raw, dict) else {}
    return DagSpec(title=str(data["title"]), tasks=tuple(tasks), models=models)


def load_dag_spec(path: Path) -> DagSpec:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"DAG file must contain a JSON object: {path}")
    return dag_spec_from_dict(data)
