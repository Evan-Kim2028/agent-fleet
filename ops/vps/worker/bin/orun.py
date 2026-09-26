"""orun.py NAME CWD PROMPT_FILE [MAX_TURNS] — one fleet agent on OpenRouter (no cmd CLI).

Drop-in for the fbrun contract: writes $R/NAME.{jsonl,out,exit,meta,err} (R = $FB_RUNS or ~/fleet/runs).
  .out  = the agent's final text
  .exit = 0 ok | 6 network/API failure | 7 hard rate limit (daily cap / credits) | 86 lazy exit | 1 other failure
Engine: agent_fleet.openrouter_backend.OpenRouterSession (read_file/write_file/edit_file/run_command/list_files
tool loop, cwd-scoped). The backend already retries HTTP 429/5xx and transport errors with backoff.
The API key comes from OPENROUTER_API_KEY in the environment and is never written anywhere.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

name, cwd, prompt_file = sys.argv[1], sys.argv[2], sys.argv[3]
max_turns = int(sys.argv[4]) if len(sys.argv) > 4 else 300
# The backend reads these at import time, so set them before importing it.
os.environ.setdefault("OPENROUTER_MAX_TOOL_ITERATIONS", str(max_turns))
os.environ.setdefault("OPENROUTER_MAX_RETRIES", "4")  # 5+10+20+40 s: rides out bursts, fails fast on a daily cap

from agent_fleet import openrouter_backend as orb  # noqa: E402

MODEL = os.environ.get("FB_MODEL_OR", "stealth/space-bunny-alpha")
R = Path(os.environ.get("FB_RUNS", str(Path.home() / "fleet" / "runs")))
R.mkdir(parents=True, exist_ok=True)
paths = {k: R / f"{name}.{k}" for k in ("jsonl", "out", "exit", "meta", "err")}
for k in ("out", "exit"):
    paths[k].unlink(missing_ok=True)
paths["meta"].write_text(json.dumps({"name": name, "cwd": cwd, "prompt": prompt_file, "engine": "openrouter",
                                     "model": MODEL, "start": int(time.time())}) + "\n")
log = paths["jsonl"].open("w")

KEY_RE = re.compile(r"sk-or-v1-[A-Za-z0-9]+")


def redact(text: str) -> str:
    return KEY_RE.sub("[REDACTED]", text or "")


# Count and log every HTTP request (the tool loop calls _call_openrouter_raw once per model turn).
requests_made = 0
_raw = orb._call_openrouter_raw


def _counting_raw(*args, **kwargs):
    global requests_made
    requests_made += 1
    t = time.monotonic()
    try:
        resp = _raw(*args, **kwargs)
        usage = resp.get("usage") if isinstance(resp, dict) else None
        log.write(json.dumps({"type": "request", "n": requests_made, "ok": True,
                              "s": round(time.monotonic() - t, 2), "usage": usage}) + "\n")
        return resp
    except Exception as exc:
        log.write(json.dumps({"type": "request", "n": requests_made, "ok": False,
                              "s": round(time.monotonic() - t, 2), "error": redact(str(exc))[:400]}) + "\n")
        raise
    finally:
        log.flush()


orb._call_openrouter_raw = _counting_raw


def classify(result) -> int:
    err = result.stderr or ""
    if result.exit_code == 0:
        # Lazy exit: no tool calls at all and the text reads as a refusal / request for the task.
        if not result.mcp_tool_calls and re.search(
                r"(?i)what would you like|how can i help|I can't|I cannot|I'm unable|please provide|paste the task",
                result.stdout or ""):
            return 86
        return 0
    if re.search(r"(?i)HTTP 429|HTTP 402", err) and re.search(
            r"(?i)per-day|per day|daily|credits|insufficient|limit exceeded", err):
        return 7
    if re.search(r"(?i)HTTP 429|HTTP 5\d\d|transport error|timed out|timeout|Connection|Name or service|"
                 r"Temporary failure", err):
        return 6
    return 1


t0 = time.monotonic()
prompt = Path(prompt_file).read_text()
backend = orb.OpenRouterBackend(model=MODEL)
session = backend.create_session(persona_name="fleet", cwd=Path(cwd), model=MODEL)
try:
    result = session.send(prompt, max_tokens=int(os.environ.get("FB_OR_MAX_TOKENS", "32768")),
                          timeout_s=int(os.environ.get("FB_OR_TIMEOUT_S", "14400")))
finally:
    session.dispose()

code = classify(result)
paths["out"].write_text(result.stdout or "")
paths["err"].write_text(redact(result.stderr or ""))
log.write(json.dumps({"type": "result", "exit": code, "backend_exit": result.exit_code, "requests": requests_made,
                      "tool_calls": len(result.mcp_tool_calls), "usage": result.usage,
                      "duration_s": round(time.monotonic() - t0, 1), "finalText": (result.stdout or "")[:2000]}) + "\n")
log.close()
paths["exit"].write_text(f"{code}\n")
sys.exit(code)
