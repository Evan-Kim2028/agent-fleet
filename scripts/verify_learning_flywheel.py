#!/usr/bin/env python3
"""
Verification for the self-improving flywheel.

**LESS IS MORE:** This file is intentionally minimal.

Instead of a custom harness, **always use the existing superpowers skill**:

    superpowers:verification-before-completion

When you (or any agent) want to verify a learning cycle or claim the flywheel is working:

1. Read the skill: agent_fleet/base-kit/superpowers/verification-before-completion/SKILL.md
2. Follow its Iron Law: No completion claims without fresh verification evidence.
3. Use it to check synthesis logs in ~/.agent-fleet/learning/synthesis_logs/
4. Use it before claiming "the flywheel works".

The actual implementation lives in:
- The `fleet-learner` persona (see personas/fleet-learner.md)
- The dispatcher trigger (see learning/ and how it calls into level_up)
- Existing level_up machinery for gating/promotion

Run learning cycles with:
    agent-fleet learn

Then apply verification-before-completion before making any claims about results.

See also:
- superpowers:systematic-debugging (when the flywheel isn't behaving)
- superpowers:writing-skills (when improving the fleet-learner persona)
- superpowers:subagent-driven-development (recommended pattern for the meta-learning loop itself)
"""

from __future__ import annotations

import sys

# This script now exists only as a pointer to the proper skills.
# The previous custom implementation has been removed in favor of using
# the vendored Cursor superpowers skills (less is more).


def main() -> int:
    print("This verification script has been reduced per 'less is more'.")
    print("Read: agent_fleet/base-kit/superpowers/verification-before-completion/SKILL.md")
    print("Then run: agent-fleet learn")
    print("Apply the skill before claiming any results about the flywheel.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
