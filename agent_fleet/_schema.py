"""Schema loading helper shared by all contract modules.

Caches schema reads with lru_cache so repeated validation in hot paths
doesn't re-read JSON from disk.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

_SCHEMA_DIR = Path(__file__).parent / "schemas"
_VALID_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


@cache
def load_schema(name: str) -> dict[str, Any]:
    """Return parsed JSON schema for *name* (e.g. "task_spec"). Cached.

    *name* is restricted to ``[a-z][a-z0-9_]*`` to prevent path traversal.
    """
    if not _VALID_NAME.fullmatch(name):
        raise ValueError(f"invalid schema name: {name!r}")
    return json.loads((_SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
