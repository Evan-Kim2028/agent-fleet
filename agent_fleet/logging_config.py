"""Shared logging setup for fleet CLIs and watchers."""

from __future__ import annotations

import logging


def configure_fleet_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
