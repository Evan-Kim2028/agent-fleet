"""Kimi Code CLI backend — optional alternative to Cursor SDK."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path


def _find_kimi_bin() -> str:
    for candidate in (shutil.which("kimi-cli"), os.path.expanduser("~/.local/bin/kimi-cli")):
        if candidate and Path(candidate).exists():
            return candidate
    return "kimi-cli"


def call_kimi(
    prompt: str,
    *,
    api_key: str,
    work_dir: str,
    timeout: int = 720,
    model: str = "kimi-for-coding",
    kimi_bin: str | None = None,
) -> str:
    """Run kimi-cli with an isolated config. Returns plain text output."""
    bin_path = kimi_bin or _find_kimi_bin()
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = os.path.join(tmpdir, "config.toml")
        with open(config_path, "w", encoding="utf-8") as handle:
            handle.write(
                textwrap.dedent(
                    f"""\
                default_model = "{model}"
                default_thinking = true
                merge_all_available_skills = true

                [providers.kimi-code]
                type = "kimi"
                base_url = "https://api.kimi.com/coding/v1"
                api_key = "{api_key}"

                [models.{model}]
                provider = "kimi-code"
                model = "{model}"
                max_context_size = 262144
            """
                )
            )

        mcp_path = os.path.join(tmpdir, "mcp.json")
        with open(mcp_path, "w", encoding="utf-8") as handle:
            json.dump({"mcpServers": {}}, handle)

        kimi_cmd = [
            bin_path,
            "--config-file",
            config_path,
            "--mcp-config-file",
            mcp_path,
            "--work-dir",
            work_dir,
            "--print",
            "--input-format",
            "text",
            "--output-format",
            "text",
            "--final-message-only",
            "--yolo",
            "--afk",
        ]

        if shutil.which("systemd-run") and (
            os.environ.get("DBUS_SESSION_BUS_ADDRESS") or os.environ.get("XDG_RUNTIME_DIR")
        ):
            kimi_cmd = [
                "systemd-run",
                "--scope",
                "--user",
                "--property=MemoryMax=8G",
                "--property=MemorySwapMax=0",
                "--collect",
                "--quiet",
            ] + kimi_cmd

        env = os.environ.copy()
        env["KIMI_API_KEY"] = api_key

        result = subprocess.run(
            kimi_cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"kimi-cli failed (exit {result.returncode}): {result.stderr[:500]}"
            )

        output = re.sub(
            r"\nTo resume this session:.*$", "", result.stdout, flags=re.MULTILINE
        ).strip()
        return output


@dataclass(frozen=True)
class KimiLLMResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_s: float
    agent_id: str | None = None


class KimiBackend:
    """Run prompts through Kimi Code CLI (subscription / KIMI_API_KEY)."""

    def __init__(
        self,
        *,
        model: str = "kimi-for-coding",
        kimi_bin: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.model = model
        self.kimi_bin = kimi_bin or _find_kimi_bin()
        self.api_key = api_key or os.environ.get("KIMI_API_KEY", "")

    def run(
        self,
        prompt: str,
        *,
        max_tokens: int,
        timeout_s: int,
        memory_limit: str = "4G",
        allowed_tools: list[str] | None = None,
        cwd: Path | None = None,
        model: str | None = None,
        mode: str | None = None,
    ) -> KimiLLMResult:
        del max_tokens, memory_limit, allowed_tools, mode

        if not self.api_key:
            return KimiLLMResult(
                stdout="",
                stderr="KIMI_API_KEY is not set",
                exit_code=1,
                duration_s=0.0,
            )

        work_dir = str(cwd or Path.cwd())
        selected_model = model or self.model
        t0 = time.monotonic()
        try:
            stdout = call_kimi(
                prompt,
                api_key=self.api_key,
                work_dir=work_dir,
                timeout=timeout_s if timeout_s > 0 else 720,
                model=selected_model,
                kimi_bin=self.kimi_bin,
            )
            return KimiLLMResult(
                stdout=stdout,
                stderr="",
                exit_code=0,
                duration_s=time.monotonic() - t0,
            )
        except Exception as exc:
            return KimiLLMResult(
                stdout="",
                stderr=str(exc),
                exit_code=1,
                duration_s=time.monotonic() - t0,
            )
