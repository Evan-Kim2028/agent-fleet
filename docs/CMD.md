# Command Code CLI backend

Agent Fleet can execute fleet runs through **Command Code** (`cmd -p`) instead of Cursor / Grok. Personas, `code_review`, worktrees, verify, and harvest are unchanged — only the execution adapter differs.

| Setting | Value |
|---|---|
| `default_backend` | `cmd` |
| Auth | `cmd login` → `~/.commandcode/auth.json` |
| Default model | `meituan/longcat-2.0:free` |
| Runtime | `cmd` on PATH (`npm i -g command-code`) |
| Taste | Apply-only. Set `cmd_taste` or use `Documents/.commandcode/taste/taste.md`. Learning is off. |
| Session | `CmdSession` first send is fresh; later sends `--resume` |

## Config

```bash
cp examples/fleet.cmd.yaml ~/.agent-fleet/fleet.yaml
```

Or per-run:

```bash
uv run agent-fleet run --backend cmd --model meituan/longcat-2.0:free \
  --workspace /path/to/repo --pipeline code_review --complexity MED \
  "Implement issue #3388. Do not commit."

`--complexity MED` (or HIGH) is required for `code_review`. Without it the goal is auto-classified; short goals become LOW and `--pipeline` is discarded.
```

`AGENT_FLEET_BACKEND=cmd` and `AGENT_FLEET_MODEL=meituan/longcat-2.0:free` also work.

## Doctor

```bash
uv run agent-fleet doctor --backend cmd
```

## Notes

- `--yolo` in agent mode; `--permission-mode plan` in plan mode.
- Turn-cap (cmd exit 8) returns the partial answer instead of failing the phase.
- Do not grow a second review loop in shell scripts — use fleet `code_review` / `auto_fix`.
- `--complexity MED` + repo `code_review.auto_fix: true` runs execute → review →
  fix (up to `max_fix_attempts`). Review stays on the CLI `--model`.
