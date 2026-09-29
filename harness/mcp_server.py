"""Serve one scenario's world over MCP (stdio, newline-delimited JSON-RPC). Stdlib only.

    python -m harness.mcp_server <scenario_id> <calls.jsonl>

Every tool call is appended to calls.jsonl; the grader replays that log on a fresh world.
"""
from __future__ import annotations

import json
import sys

from .scenarios import SCENARIOS
from .tools import TOOLS


def main() -> None:
    sid, log_path = sys.argv[1], sys.argv[2]
    world = SCENARIOS[sid].world()
    with open(log_path, "a") as log:
        for line in sys.stdin:
            if not line.strip():
                continue
            msg = json.loads(line)
            if "id" not in msg:  # notification
                continue
            method, params = msg.get("method"), msg.get("params") or {}
            reply = {"jsonrpc": "2.0", "id": msg["id"]}
            if method == "initialize":
                reply["result"] = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                                   "capabilities": {"tools": {}}, "serverInfo": {"name": "assistant", "version": "1.0"}}
            elif method == "tools/list":
                reply["result"] = {"tools": [{"name": t["name"], "description": t["description"],
                                              "inputSchema": t["input_schema"]} for t in TOOLS]}
            elif method == "tools/call":
                out, err = world.call(params["name"], params.get("arguments") or {})
                log.write(json.dumps(world.log[-1]) + "\n")
                log.flush()
                reply["result"] = {"content": [{"type": "text", "text": out}], "isError": err}
            elif method == "ping":
                reply["result"] = {}
            else:
                reply["error"] = {"code": -32601, "message": f"method not found: {method}"}
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
