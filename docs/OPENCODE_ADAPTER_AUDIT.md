# OpenCode adapter audit

Date: 2026-07-19  
Audited local version: OpenCode Desktop 1.17.13

## Outcome

Use one dependency-free global OpenCode plugin as the live adapter. The plugin receives supported
OpenCode lifecycle events inside the already-running desktop backend and translates only sanitized
state into the existing PetCrew `1.0` protocol. Do not start a second OpenCode server, install a
second CLI, or read the 1.3 GB local message database for routine monitoring.

The repository implementation is inert until the user separately approves installation into the
global OpenCode plugin directory.

## Local facts

- Desktop executable: `%LOCALAPPDATA%\Programs\@opencode-aidesktop\OpenCode.exe`.
- Installed desktop version: `1.17.13`.
- The `opencode` CLI is not available on `PATH`; this is not required for a local plugin.
- Global config: `%USERPROFILE%\.config\opencode\opencode.jsonc`.
- Standard global plugin directory: `%USERPROFILE%\.config\opencode\plugins\`.
- State root: `%USERPROFILE%\.local\share\opencode\`.
- The state root contains credentials and a large `opencode.db`; the adapter does not read either.
- Windows has an `opencode` URL protocol registered to the desktop executable, but the supported
  version audited here did not yet have a verified session-specific URI shape.
- On 2026-07-24, the installed OpenCode Desktop 1.18.3 application bundle was verified read-only:
  it accepts `opencode://open-project?directory=...` and `opencode://new-session?directory=...`,
  but no exact session-resume deep link. PetCrew therefore opens the existing project and labels
  the action accordingly.
- Live acceptance then proved that Windows had no registered `opencode` URI class, so dispatching
  the supported URI through `explorer.exe` opened Documents. The supported application argument is
  therefore passed directly to the verified standard per-user `OpenCode.exe`; registry repair and
  alternate-install discovery remain out of scope.

## Supported source

Official OpenCode plugins may subscribe to a generic `event` hook plus named tool hooks. Version
1.17.13 exposes the required stable events:

- `session.created`, `session.updated`, `session.status`, `session.idle`, `session.error`,
  `session.deleted`;
- `permission.asked`, `permission.v2.asked`, and their reply events;
- `question.asked`, `question.v2.asked`, and their reply/reject events;
- `todo.updated`;
- `tool.execute.before` and `tool.execute.after`.

Session records include stable session and parent identifiers, project/directory metadata, title,
and timestamps. `session.status` distinguishes `busy`, `idle`, and `retry`. A plugin also receives
the official SDK client, so startup can list session metadata and current status without reading
messages or the database.

## Mapping

| OpenCode signal | PetCrew event/state |
| --- | --- |
| new session | `agent.discovered` / queued |
| status `busy` | `agent.started` / working |
| tool before/after | generic `agent.activity`; arguments and results discarded |
| permission asked | `agent.attention_requested` / waiting approval |
| question asked | `agent.attention_requested` / waiting input |
| reply/reject | `agent.attention_resolved` / working |
| explicit todo list | `agent.progress` with completed/total plan steps |
| idle after live work | unread `agent.completed` result |
| session error | unread `agent.failed` result with a generic summary |
| deleted active session | unread `agent.cancelled` result |

Root and child sessions are separate cards. `parentID` becomes `parent_agent_id`; raw identifiers
are SHA-256-derived before they leave the adapter.

## Privacy and failure contract

The adapter may retain the project directory's final name, sanitized session title, generic tool
category, typed attention kind, explicit todo counts, timestamps, and opaque identifiers. The one
exception is the absolute session directory in the explicit OpenCode project-navigation target;
it is never rendered or logged and is removed before durable hub-cache persistence. The adapter
must discard prompts, message parts, reasoning, tool arguments, command text, tool output, error
bodies, all other full paths, provider credentials, and permission answers.

It atomically replaces the latest sanitized PetCrew registry record and attempts the authenticated
loopback POST with a short timeout. Missing PetCrew runtime, invalid metadata, or network failure is
a silent no-op and must never delay or alter OpenCode work.

## Activation gate and rollback

Activation is a later explicit step:

1. Back up the current global OpenCode config directory metadata and any existing `plugins` folder.
2. Show the exact repository file to be copied and its SHA-256.
3. Copy only the reviewed adapter into `~/.config/opencode/plugins/`.
4. Restart OpenCode only after separate user approval, because local plugins load at startup.
5. Verify one busy task, one question, one permission request, one child session, and completion.

Rollback is to close OpenCode, move only the PetCrew-owned plugin file out of the global plugin
directory, then reopen OpenCode. No provider, account, auth, model, permission, or existing plugin
configuration is changed.

## Sources

- OpenCode Plugins: https://opencode.ai/docs/plugins/
- OpenCode Server: https://opencode.ai/docs/server/
- OpenCode SDK: https://opencode.ai/docs/sdk/
- OpenCode 1.17.13 generated event types:
  https://github.com/anomalyco/opencode/blob/v1.17.13/packages/sdk/js/src/v2/gen/types.gen.ts
