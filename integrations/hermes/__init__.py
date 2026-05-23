"""Cursor coding fleet — Hermes plugin registration."""

import logging
from pathlib import Path
from typing import Protocol

from . import schemas, tools

logger = logging.getLogger(__name__)


class HermesPluginContext(Protocol):
    def register_tool(
        self,
        *,
        name: str,
        toolset: str,
        schema: object,
        handler: object,
        requires_env: list[str] | None = None,
    ) -> None: ...

    def register_skill(self, name: str, skill_md: Path) -> None: ...


def register(ctx: HermesPluginContext) -> None:
    ctx.register_tool(
        name="coding_fleet_dispatch",
        toolset="coding_fleet",
        schema=schemas.CODING_FLEET_DISPATCH,
        handler=tools.coding_fleet_dispatch,
        requires_env=["CURSOR_API_KEY"],
    )
    ctx.register_tool(
        name="coding_fleet_list_personas",
        toolset="coding_fleet",
        schema=schemas.CODING_FLEET_LIST_PERSONAS,
        handler=tools.coding_fleet_list_personas,
    )
    skills_dir = Path(__file__).parent / "skills"
    if skills_dir.is_dir():
        for child in sorted(skills_dir.iterdir()):
            skill_md = child / "SKILL.md"
            if child.is_dir() and skill_md.exists():
                ctx.register_skill(child.name, skill_md)
    logger.info("cursor-fleet plugin registered (coding_fleet toolset)")
