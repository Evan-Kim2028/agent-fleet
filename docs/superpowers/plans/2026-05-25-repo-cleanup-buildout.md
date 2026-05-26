# Repo Cleanup Buildout

**Goal:** Make agent-fleet easier to reason about, maintain, and self-dispatch without losing work.

**Base branch:** `feature/skills-pr-loop` (stack through PR3). Commit on `feature/repo-cleanup` or topic sub-branches.

## Workstreams (7 parallel personas)

| ID | Persona | Tasks |
|----|---------|-------|
| W1 | cleanup-worktree | 1,2,6,13,16,17 — worktree retention, commit-before-teardown, base ref, integration test, overlap warning, auto_cleanup docs |
| W2 | cleanup-dispatch-tooling | 3,5,15 — harvest script, sequential-stack flag, dispatch cookbook |
| W3 | cleanup-stack-pr4 | 4 — integrate PR4 loadouts from `task-2-6316c6c3` |
| W4 | cleanup-equip-audit | 7,8 — audit backend.run equip bypass; default auto_fix in examples |
| W5 | cleanup-skills-canonical | 9,12 — PR5 thermo-nuclear id + lean loadout defaults |
| W6 | cleanup-config | 10,11 — fleet.yaml personas_dir, import shadow guard |
| W7 | cleanup-test-gaps | 14 — PR3 review test gaps |

## Task checklist

- [ ] 1. Land `should_keep_task_worktree` + tests on branch
- [ ] 2. Auto-commit fleet work before teardown on recoverable statuses
- [ ] 3. `scripts/harvest-fleet-worktree.sh`
- [ ] 4. PR4 loadouts committed on `feature/skills-personas`
- [ ] 5. `--sequential-stack` on dispatch scripts
- [ ] 6. `default_branch` / `base_branch` wired into `prepare_task_workspace`
- [ ] 7. Equip audit doc + fixes for stray `backend.run()` paths
- [ ] 8. `code_review.auto_fix` defaults in fleet.example.yaml
- [ ] 9. PR5 canonical skill ids
- [ ] 10. Fix global `personas_dir` + repo template
- [ ] 11. Import shadow check script + README warning
- [ ] 12. Curated loadouts (no 20-skill prompt bloat)
- [ ] 13. Dispatcher worktree retention integration test
- [ ] 14. Fast-path + CI journal test gaps
- [ ] 15. `docs/DISPATCH-COOKBOOK.md`
- [ ] 16. Parallel scope overlap detector/warning
- [ ] 17. Document `worktree.auto_cleanup` vs dispatcher `keep=`
