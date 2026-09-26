#!/usr/bin/env bash
# pr_triage.sh [--apply] — deterministic (no model) triage of open fb/* PRs; see pr_triage.py for the rules.
# Dry-run by default: prints the decision table. --apply closes ON-MAIN / SUPERSEDED / DEAD PRs with evidence
# comments and launches rebase_regate.sh for CONFLICTING ones (max 4 concurrent).
F=${FLEET_OPS_HOME:-$HOME/fleet/ops}
exec python3 $F/pr_triage.py "$@"
