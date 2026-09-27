---
name: use-penpot-on-demand
description: Start, use, and stop Lisa's existing local Penpot MCP only for an explicitly requested Penpot design session. Use when Lisa invokes Penpot or asks to work in an existing Penpot file; do not activate for ordinary visual work that does not require Penpot.
---

# Penpot по требованию

Do not keep a global Codex entry `mcp_servers.penpot`, even when it is disabled. This skill uses the
existing scheduled task `Tribunska Penpot MCP` and a bundled local Streamable HTTP client, so Codex
App Server never owns or probes the Penpot connection.

## Start

1. Read the target project's visual rules before changing a design. For Atelier or Tribune, also
   read the existing Penpot workflow referenced by that project's instructions.
2. Run `scripts/manage_penpot.ps1 -Mode status`. Never infer availability from a task name alone.
3. When the user explicitly requested Penpot work, run `scripts/manage_penpot.ps1 -Mode start`.
   This starts only the existing scheduled task and waits at most 120 seconds for loopback port
   4401. It must not create a second server or download packages.
4. If the controller reports `cache_missing`, stop and request approval for the exact pinned
   `@penpot/mcp` package restoration. Do not substitute `latest` or silently install anything.
5. Penpot's own MCP plugin must be open and connected in the target design file. If it is not,
   surface that one user action. Do not claim readiness from port 4401 alone.

## Use

- Run `python scripts/penpot_mcp_client.py list` to obtain the live tool schema.
- For a tool call, place one JSON object with the arguments in a temporary UTF-8 file and run
  `python scripts/penpot_mcp_client.py call <tool-name> --arguments-file <path>`.
- Keep the temporary file inside the current writable workspace and remove only that file after
  the call. Never put credentials, environment variables, or unrelated local data in it.
- Prefer semantic names over stored Penpot UUIDs. Before changing an accepted board, export or
  preserve the current version, clone it, and make a new version unless Lisa explicitly approves an
  in-place change.
- After any uncertain timeout or disconnect, inspect the current Penpot file before repeating a
  mutation. A failed client response does not prove the design action failed.

The local MCP can execute design code inside the Penpot plugin. Treat mutating calls as real design
changes: keep them within the user's requested file and scope, and verify the resulting layers and
export rather than trusting the tool response alone.

## Stop

At the end of the Penpot work, run `scripts/manage_penpot.ps1 -Mode stop` only when this skill
started the scheduled task. If the server was already running, leave it alone. The controller stops
only the exact scheduled task; if a verified Penpot listener survives, it reports the PID and stops
without killing it. Never kill by process name, wildcard, or port sweep.

Report whether the server was started, whether the Penpot plugin connected, what design was changed
or inspected, and whether the scheduled task was stopped. Do not add or enable a global Codex
Penpot MCP entry as a convenience workaround.

Official runtime contract: https://github.com/penpot/penpot/blob/develop/mcp/README.md
