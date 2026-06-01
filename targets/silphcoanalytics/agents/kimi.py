"""Kimi for Coding backend — subprocess wrapper for kimi-cli."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import agents.logging as aglog

def _find_kimi_bin() -> str:
    # Search for kimi-cli explicitly. Do NOT search for plain "kimi" — it's too
    # generic and can collide with unrelated tools in shared PATH entries.
    candidates = [
        shutil.which("kimi-cli"),
        os.path.expanduser("~/.local/bin/kimi-cli"),
    ]
    for p in candidates:
        if p and Path(p).exists():
            return p
    return "kimi-cli"


KIMI_BIN: str = _find_kimi_bin()


def call_kimi(prompt: str, api_key: str, work_dir: str, timeout: int = 720) -> str:
    """Run kimi-cli with an isolated config. Returns plain text output."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = os.path.join(tmpdir, "config.toml")
        with open(config_path, "w") as f:
            f.write(textwrap.dedent(f"""\
                default_model = "kimi-for-coding"
                default_thinking = true
                merge_all_available_skills = true

                [providers.kimi-code]
                type = "kimi"
                base_url = "https://api.kimi.com/coding/v1"
                api_key = "{api_key}"

                [models.kimi-for-coding]
                provider = "kimi-code"
                model = "kimi-for-coding"
                max_context_size = 262144
            """))

        mcp_path = os.path.join(tmpdir, "mcp.json")
        with open(mcp_path, "w") as f:
            json.dump({"mcpServers": {}}, f)

        kimi_cmd = [
            KIMI_BIN,
            "--config-file", config_path,
            "--mcp-config-file", mcp_path,
            "--work-dir", work_dir,
            "--print", "--input-format", "text",
            "--output-format", "text", "--final-message-only",
            "--yolo", "--afk",
        ]

        # Only wrap with systemd-run when the user session bus is reachable
        # ($DBUS_SESSION_BUS_ADDRESS or $XDG_RUNTIME_DIR must be set)
        if shutil.which("systemd-run") and (
            os.environ.get("DBUS_SESSION_BUS_ADDRESS") or os.environ.get("XDG_RUNTIME_DIR")
        ):
            kimi_cmd = [
                "systemd-run", "--scope", "--user",
                "--property=MemoryMax=8G",
                "--property=MemorySwapMax=0",
                "--collect", "--quiet",
            ] + kimi_cmd

        env = os.environ.copy()
        env["KIMI_API_KEY"] = api_key

        aglog.log("kimi", f"Calling kimi-cli (work_dir={work_dir}, timeout={timeout}s)...")
        result = subprocess.run(
            kimi_cmd, input=prompt, capture_output=True, text=True,
            timeout=timeout, env=env,
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"kimi-cli failed (exit {result.returncode}): {result.stderr[:500]}"
            )

        output = result.stdout
        output = re.sub(r"\nTo resume this session:.*$", "", output, flags=re.MULTILINE).strip()
        aglog.log("kimi", f"Response: {len(output)} chars")
        return output
