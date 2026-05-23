#!/usr/bin/env python3
"""Shim — delegates to agent_fleet.pr_review.github_action."""

from agent_fleet.pr_review.github_action import main

if __name__ == "__main__":
    raise SystemExit(main())
