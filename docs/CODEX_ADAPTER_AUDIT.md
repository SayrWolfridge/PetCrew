# Codex read-only adapter audit

Date: 2026-07-18

## Scope

This audit identifies a supported way for PetCrew to observe Codex desktop work without patching the application, reading private transcripts, replacing the existing pet, or changing approval behavior.

No Codex hook, plugin, marketplace entry, MCP server, or configuration value was installed or changed during the audit.

## Evidence

- Installed desktop package: `OpenAI.Codex 26.715.2305.0`.
- The running desktop application owns a child `codex.exe app-server` process with no published `--listen` transport. Its stdio channel is private to the desktop host.
- `~/.codex/config.toml` exists and already defines `notify`; it has no hook section. `~/.codex/hooks.json` does not exist.
- The official [Hooks documentation](https://learn.chatgpt.com/docs/hooks) documents `SessionStart`, `PreToolUse`, `PermissionRequest`, `PostToolUse`, `SubagentStart`, `SubagentStop`, and `Stop`. Command hooks are synchronous; asynchronous command hooks are not supported yet. Non-managed hooks require explicit trust review.
- The official [App Server documentation](https://learn.chatgpt.com/docs/app-server) exposes rich thread, turn, plan, item, approval, and collaboration events to clients that own an app-server connection.
- The open-source [App Server protocol](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md) includes `turn/plan/updated`, `collabToolCall`, spawned-thread relationships, agent nicknames, and thread status.
- A current open Codex issue documents that ordinary hook inputs do not reliably attribute tool lifecycle events to individual subagents: [openai/codex#16226](https://github.com/openai/codex/issues/16226).
- Plugins can bundle hooks, an MCP server, and a skill under one reviewable package; see [Build plugins](https://learn.chatgpt.com/docs/build-plugins).

## Findings

### Hooks

Hooks are the supported automatic observation point for work already started in the desktop app.

They can provide:

- root task start and stop;
- generic current activity from tool names;
- visible permission or input attention without answering it;
- best-effort subagent start and stop.

They cannot provide truthful `4/10` progress unless the agent explicitly reports a real plan. Current hook payloads also do not reliably map every child tool call to one unique subagent. PetCrew must not invent identities or merge concurrent children by display name.

Confidence: 95% that hooks can provide a useful root-task baseline; 95% that hooks alone do not satisfy exact per-subagent live attribution today.

### App Server

App Server is the richest technical source. It has the exact turn, item, plan, collaboration, and child-thread fields PetCrew wants.

However, the running desktop instance exposes its App Server over private stdio, not a documented external listener. Starting a second App Server would create or own a separate runtime and would not be a supported observer connection to the already running desktop tasks. PetCrew must not copy the packaged `codex.exe`, install a second CLI, shim it, or scrape its private stdio.

Confidence: 95% that App Server is the correct future deep-integration protocol; 90% that the current desktop process cannot be safely attached to by an external PetCrew process.

### `notify`

The existing `notify` setting is completion-oriented and already user-owned. It is not sufficient for live activity, attention, child-agent identity, or progress. PetCrew must preserve it unchanged.

### Private state and UI scraping

Polling rollouts, SQLite, logs, transcripts, or the existing two-card pet UI is rejected. Those sources are private or incomplete, may expose sensitive data, and do not meet the truthful ten-agent requirement.

## Recommended first adapter

Build a local Codex plugin, but do not install it during implementation.

```text
plugins/petcrew/
  .codex-plugin/plugin.json
  hooks/hooks.json
  .mcp.json
  scripts/petcrew_bridge.py
  skills/petcrew-report-status/SKILL.md
```

The plugin has two complementary lanes:

1. **Automatic hook lane** - root lifecycle, generic sanitized activity, attention, completion, and best-effort child lifecycle.
2. **Explicit semantic reporter lane** - an optional local MCP tool used by roots and subagents to report stable identity, parent identity, readable action, real `current/total`, and completion summary.

The bridge sends only normalized events to the already authenticated PetCrew loopback hub. It never returns an approval decision, changes tool input, or reads the transcript path.

### Mapping

| Codex source | PetCrew event | Rule |
| --- | --- | --- |
| `SessionStart` | none | Warm-up/no-op because it has no turn identity and must not create a misleading card. |
| `UserPromptSubmit` | `agent.started` | One root task keyed by stable `session_id` plus `turn_id`; discard the prompt. |
| `PreToolUse` / `PostToolUse` | `agent.activity` | Map allow-listed tool names to generic Russian actions; discard arguments and output. |
| `PermissionRequest` | `agent.attention_requested` | Display only; return no decision. |
| `Stop` | `agent.completed` | Short generic result unless an explicit reporter supplied a safe summary. |
| `SubagentStart` / `SubagentStop` | child lifecycle | Emit a unique child only when the payload supplies stable `agent_id`; otherwise emit no child card. |
| `petcrew_report_status` | progress/activity/completion | Accept explicit identity and truthful semantic plan fields. |

## Failure isolation

- Hook handlers return empty success output and never use `continue: false`.
- Each hook has a one-second hard timeout and a smaller internal HTTP deadline.
- PetCrew closed or unavailable means a quick no-op; Codex work continues.
- The bundled MCP server is optional, never `required`.
- The bridge connects only to the dynamic `127.0.0.1` hub endpoint and reads its per-user runtime descriptor and secret.
- Raw prompts, tool arguments, command bodies, tool results, transcripts, environment variables, and authorization values are neither transmitted nor persisted.

## Exact future installation surface

Implementation may add these inert, version-controlled files inside the repository without changing Codex:

- `plugins/petcrew/**`;
- `.agents/plugins/marketplace.json`, with one local marketplace entry named `petcrew` pointing to `./plugins/petcrew`.

Only after a separate user approval may installation change live state:

1. Create timestamped copies of `~/.codex/config.toml` and any pre-existing personal marketplace file.
2. Install `petcrew` from the PetCrew repository marketplace in the desktop Plugins browser.
3. Verify that the only new configuration section is the PetCrew plugin enablement entry; preserve `notify` and every existing plugin section byte-for-byte.
4. Review and trust the exact PetCrew hook hash through `/hooks`.
5. Start a new task or restart the desktop app only at a user-chosen time.
6. Test one root task, three parallel root tasks, and three parallel subagents before enabling broader use.

No MFA, restart, sign-out, or provider authentication action is authorized by this audit.

## Rollback

1. Disable PetCrew in the Plugins browser.
2. Uninstall the PetCrew plugin.
3. Confirm that only the PetCrew plugin enablement entry and its cached bundle were removed.
4. If UI removal is incomplete, restore the timestamped configuration copy and remove only the PetCrew marketplace entry/cache path after showing the exact targets.
5. Start a new task or restart at a user-chosen time and verify Codex without PetCrew.
6. Keep the standalone PetCrew simulator available; rollback never removes the application or its project source.

## Decision

The inert plugin bundle is implemented and tested inside the PetCrew repository. Do not install or enable it until the hook commands, MCP launch, exact config diff, and rollback commands have been reviewed with the user.
