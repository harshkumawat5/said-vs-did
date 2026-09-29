"""Backend that runs the agent through the Claude Code CLI (`claude -p`) on the user's Claude login.

Same system prompt and the same 14 tools as agent.py, served by harness/mcp_server.py. Claude Code's built-in
tools are switched off, and the child runs in an empty directory with a minimal environment and no setting
sources, so no CLAUDE.md, hooks or user settings reach it. The model sees the tools as mcp__assistant__<name>
("workspace" is a reserved server name in Claude Code). Claude Code still adds a short preamble, an environment
block with today's date, and the account's email as context; use the api backend for a clean context.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from .agent import SYSTEM

ROOT = Path(__file__).resolve().parent.parent
CLAUDE = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"  # Opus 5.5 needs Claude Code >= 2.1.280
MODEL_IDS = {"haiku-4.5": "claude-haiku-4-5", "sonnet-5.5": "claude-sonnet-5-5", "opus-5.5": "claude-opus-5-5"}
EFFORT = {"sonnet-5.5": "medium", "opus-5.5": "medium"}
MAX_TURNS = 25


def clean_env() -> dict:
    keep = ("HOME", "USER", "LOGNAME", "PATH", "LANG", "TMPDIR", "SHELL")
    return {k: os.environ[k] for k in keep if k in os.environ} | {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}


async def claude(args: list[str], cwd: Path, timeout: float = 900) -> list[dict]:
    proc = await asyncio.create_subprocess_exec(CLAUDE, *args, cwd=cwd, env=clean_env(),
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("claude -p timed out")
    events = [json.loads(line) for line in out.decode().splitlines() if line.startswith("{")]
    res = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if res is None:
        raise RuntimeError(f"claude -p exited {proc.returncode} without a result: {err.decode()[-500:]}")
    if res.get("is_error") and res.get("api_error_status"):
        raise RuntimeError(f"claude -p API error {res['api_error_status']}: {str(res.get('result'))[:300]}")
    return events


async def run_agent_cli(model: str, scenario_id: str, instruction: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="svd-") as tmp:
        cwd, log = Path(tmp), Path(tmp) / "calls.jsonl"
        log.touch()
        cfg = {"mcpServers": {"assistant": {"type": "stdio", "command": sys.executable,
                                            "args": ["-m", "harness.mcp_server", scenario_id, str(log)],
                                            "env": {"PYTHONPATH": str(ROOT)}}}}
        args = ["-p", instruction, "--model", MODEL_IDS[model], "--system-prompt", SYSTEM,
                "--mcp-config", json.dumps(cfg), "--strict-mcp-config", "--tools", "",
                "--allowedTools", "mcp__assistant", "--setting-sources", "", "--no-session-persistence",
                "--max-turns", str(MAX_TURNS), "--output-format", "stream-json", "--verbose"]
        if model in EFFORT:
            args += ["--effort", EFFORT[model]]
        events = await claude(args, cwd)
        calls = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]

    res = next(e for e in reversed(events) if e.get("type") == "result")
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    transcript = [dict(role=e["type"], content=e["message"]["content"]) for e in events
                  if e.get("type") in ("assistant", "user") and "message" in e]
    u = res.get("usage", {})
    return dict(final_text=(res.get("result") or "").strip(), stop_reason=res.get("subtype"),
                steps=res.get("num_turns"),
                usage={k: u.get(k, 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                                "cache_read_input_tokens")},
                cost_usd=round(res.get("total_cost_usd", 0.0), 5), seconds=round(res.get("duration_ms", 0) / 1000, 1),
                backend="claude-code", tools_seen=init.get("tools"), transcript=transcript, tool_calls=calls)


async def ask_json(prompt: str, schema: dict, model: str = "claude-opus-5-5", effort: str = "low") -> dict:
    """One structured-output call through the CLI (used for claim extraction)."""
    with tempfile.TemporaryDirectory(prefix="svd-") as tmp:
        events = await claude(["-p", prompt, "--model", model, "--tools", "", "--strict-mcp-config",
                               "--setting-sources", "", "--no-session-persistence", "--effort", effort,
                               "--system-prompt", "You extract structured data exactly as instructed.",
                               "--json-schema", json.dumps(schema), "--output-format", "json"], Path(tmp), 300)
    res = next(e for e in reversed(events) if e.get("type") == "result")
    if res.get("structured_output") is not None:
        return res["structured_output"]
    return json.loads(res["result"])
