#!/usr/bin/env python3
"""Entrypoint for PR loop watcher (systemd-friendly)."""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent Fleet PR loop watcher")
    parser.add_argument("--workspace", default=str(Path.cwd()), help="Repo path")
    parser.add_argument("--once", action="store_true", help="Poll once and exit")
    args = parser.parse_args()

    from agent_fleet.cli import cmd_loop

    ns = argparse.Namespace(
        workspace=args.workspace,
        once=args.once,
        pr_number=None,
        branch=None,
        skip_review_wait=False,
        config=None,
    )
    return cmd_loop(ns)


if __name__ == "__main__":
    raise SystemExit(main())
