"""RepoContext contract: forward already-explored repo knowledge between phases.

Built once after RESEARCH from the union of research-note referenced_files
plus the task scope. Rendered into a bounded prompt block so SYNTHESIZE /
IMPLEMENT / REVIEW do not re-explore the codebase from scratch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import jsonschema

from agent_fleet._schema import load_schema

_MAX_FILES_RENDERED = 100


@dataclass(frozen=True)
class RepoContext:
    scope_paths: tuple[str, ...]
    referenced_files: tuple[str, ...]
    notes: str

    @classmethod
    def from_research(
        cls,
        scope_paths: list[str],
        research_notes: list[Any],
        notes: str = "",
    ) -> RepoContext:
        """Build from task scope + the union of note.referenced_files.

        *research_notes* items must expose a ``referenced_files`` iterable
        (ResearchNote does).
        """
        files: set[str] = set()
        for n in research_notes:
            for f in getattr(n, "referenced_files", []):
                if f:
                    files.add(f)
        return cls(
            scope_paths=tuple(dict.fromkeys(scope_paths)),
            referenced_files=tuple(sorted(files)),
            notes=notes,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_paths": list(self.scope_paths),
            "referenced_files": list(self.referenced_files),
            "notes": self.notes,
        }

    def render(self) -> str:
        """Bounded prompt block. Empty string when there is nothing to say."""
        if not self.referenced_files and not self.scope_paths:
            return ""
        lines = [
            "## Known repository context",
            "These files were already explored in an earlier phase. Trust "
            "this list; do NOT re-explore the whole tree unless you suspect "
            "it is stale or incomplete.",
            "",
            "Scope paths:",
        ]
        for p in self.scope_paths:
            lines.append(f"  * {p}")
        if self.referenced_files:
            lines.append("")
            lines.append("Files already read:")
            for f in self.referenced_files[:_MAX_FILES_RENDERED]:
                lines.append(f"  - {f}")
            extra = len(self.referenced_files) - _MAX_FILES_RENDERED
            if extra > 0:
                lines.append(f"    (... and {extra} more)")
        if self.notes:
            lines += ["", "Notes:", self.notes]
        return "\n".join(lines)


def validate_repo_context(data: dict[str, Any]) -> None:
    """Raise jsonschema.ValidationError if data does not match the schema."""
    jsonschema.validate(instance=data, schema=load_schema("repo_context"))
