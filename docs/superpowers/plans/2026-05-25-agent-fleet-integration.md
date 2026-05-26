# Agent-Fleet Integration Plan

Dispatch via workstreams (not ad-hoc scripts):

```bash
agent-fleet workstream run registry          # sequential: do this first
agent-fleet workstream run scout-loadouts skills-stack --parallel
```

See [WORKSTREAMS.md](../../WORKSTREAMS.md).
