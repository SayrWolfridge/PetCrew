from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "opencode_bridge.py"
MANIFEST = ROOT / ".mcp.json"


def manifest_environment() -> dict[str, str]:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    server = payload["mcpServers"]["opencode_bridge"]
    configured = server.get("env", {})
    if not isinstance(configured, dict):
        raise RuntimeError("invalid MCP environment")
    environment = os.environ.copy()
    environment.update({str(key): str(value) for key, value in configured.items()})
    return environment


def exchange(process: subprocess.Popen[str], message: dict) -> dict:
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    process.stdin.flush()
    line = process.stdout.readline()
    if not line:
        raise RuntimeError("MCP bridge closed stdout unexpectedly")
    return json.loads(line)


def main() -> int:
    process = subprocess.Popen(
        [sys.executable, str(SCRIPT), "mcp"],
        cwd=str(ROOT),
        text=True,
        encoding="utf-8",
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=manifest_environment(),
    )
    try:
        initialized = exchange(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26"},
            },
        )
        if initialized["result"]["serverInfo"]["name"] != "opencode-bridge":
            raise RuntimeError("unexpected MCP server identity")
        listed = exchange(
            process,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        names = {tool["name"] for tool in listed["result"]["tools"]}
        expected = {
            "codex_bind_return",
            "codex_confirm_return",
            "codex_rollback_return",
            "codex_detach_return",
            "codex_get_return_parent",
            "codex_transfer_return_parent",
            "codex_return_status",
            "codex_read_return",
            "opencode_health",
            "opencode_list_sessions",
            "opencode_read_session",
            "opencode_start_task",
            "opencode_continue",
            "opencode_detach",
            "opencode_wait",
            "opencode_answer_question",
            "opencode_reply_permission",
            "opencode_abort",
        }
        if names != expected:
            raise RuntimeError(f"unexpected MCP tools: {sorted(names)}")
        tools = {tool["name"]: tool for tool in listed["result"]["tools"]}
        start_schema = tools["opencode_start_task"]["inputSchema"]
        if start_schema["properties"].get("model", {}).get("type") != "string":
            raise RuntimeError("new-task model selection is missing from the installed MCP schema")
        if "model" in start_schema["required"]:
            raise RuntimeError("new-task model selection must remain optional")
        if "model" in tools["opencode_continue"]["inputSchema"]["properties"]:
            raise RuntimeError("follow-up schema must preserve the existing model")
        health = exchange(
            process,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "opencode_health", "arguments": {}},
            },
        )
        structured = health["result"]["structuredContent"]
        if structured.get("healthy") is not True:
            raise RuntimeError(f"OpenCode health failed: {structured}")
        expected_server = manifest_environment().get("OPENCODE_BRIDGE_EXTERNAL_URL")
        if structured.get("server") != expected_server:
            raise RuntimeError(
                f"MCP probe escaped configured server: {structured.get('server')}"
            )
        shutdown = exchange(
            process,
            {"jsonrpc": "2.0", "id": 4, "method": "shutdown"},
        )
        if shutdown["result"] != {}:
            raise RuntimeError("unexpected shutdown response")
        print(
            json.dumps(
                {
                    "mcp": "ok",
                    "tools": len(names),
                    "opencode": structured.get("version"),
                    "server": structured.get("server"),
                    "new_task_model_selection": True,
                },
                ensure_ascii=False,
            )
        )
        return 0
    finally:
        if process.stdin:
            process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        if process.returncode not in {0, None}:
            error = process.stderr.read() if process.stderr else ""
            raise RuntimeError(f"MCP bridge exited {process.returncode}: {error}")


if __name__ == "__main__":
    raise SystemExit(main())
