# Security boundaries

## Default posture

PetCrew begins as a read-only, local-only observer. Convenience must not weaken Codex or OpenCode approval boundaries.

## Live configuration

During architecture and simulator phases PetCrew must not modify:

- `~/.codex/config.toml`;
- `~/.codex/hooks.json`;
- project `.codex` directories;
- OpenCode plugin or configuration directories;
- Codex installation files;
- installed custom pet assets.

Before a future adapter installation:

1. Inspect existing configuration.
2. Show the exact proposed addition.
3. Create a timestamped rollback copy.
4. Merge only PetCrew-owned entries.
5. Validate syntax and application startup.
6. Uninstall by removing only PetCrew-owned entries.

The approved design surface is a repository marketplace plus a local PetCrew plugin. Building these inert files inside the repository does not authorize installation. Live installation additionally requires:

- a timestamped copy of `~/.codex/config.toml` and any pre-existing personal marketplace file;
- a reviewed plugin manifest, MCP configuration, and exact hook hash;
- proof that existing `notify`, MCP, feature, and plugin entries remain unchanged;
- user-controlled plugin installation and hook trust review;
- a user-chosen new-task or restart window;
- an uninstall test that removes only PetCrew-owned config and cache paths.

The hook bridge must use empty successful output, never return `continue: false`, and enforce a one-second outer timeout plus a shorter hub request deadline. The MCP reporter is optional and must not be marked `required`.

## Local transport

- Bind to `127.0.0.1` only.
- Generate a per-install random secret.
- Authenticate every event writer.
- Set strict body-size and request-time limits.
- Reject malformed and replayed events.
- Do not expose a LAN listener in the MVP.

Implemented v1 limits:

- dynamic loopback port, never `0.0.0.0`;
- bearer token with 256 bits of randomness, persisted in the current user's Tauri local-data directory;
- runtime discovery metadata and the secret are separate files;
- maximum request body: 64 KiB;
- maximum request time: two seconds;
- replay protection by `event_id` and monotonically increasing per-agent `sequence`;
- CORS allowlist limited to the packaged Tauri origin and the fixed local Vite development origin;
- no raw authorization header, event body, prompt, transcript, command body, or environment dump is logged.

PetCrew Core is the only process allowed to own `hub-runtime.json`, `hub-cache.json`, the agent
registry import loop, and Codex recovery reader. PetCrew Monitor receives the bearer token through
its bundled native boundary and may use it only for the snapshot feed, local acknowledgement, and
the explicit demo writer. The token is never embedded in frontend assets, a URL, or an SSE query
parameter. Starting a standalone Core while an authenticated healthy Core already owns the runtime
descriptor must fail instead of creating a second watcher.

The bundled UI may request the connection token through a Tauri command solely to exercise the authenticated demo writer. Remote web content is not allowed by CSP and cannot use that command.

## Data minimization

Persist only what the UI needs:

- agent identity and parent relationship;
- project display name;
- phase and progress;
- short semantic status;
- short completion summary;
- timestamps and acknowledgement state.

The global Codex hook adapter stores one bounded JSON record per agent under
`%LOCALAPPDATA%\app.petcrew.overlay\agent-registry\`. Registry identifiers are SHA-256-derived
opaque values. The only project label is the final directory name of the hook `cwd`; the full path
is never stored. Each file is atomically replaced, limited to 64 KiB, and contains the latest
already-sanitized protocol event only. PetCrew imports at most 500 records.

Active registry records expire after 24 hours without an event. Terminal registry files expire
after seven days; once imported, ordinary acknowledged-result retention applies in the hub cache.
The local `Очистить` action removes the PetCrew-owned registry and hub cache. Hook failure or
registry write failure never blocks Codex work.

Existing-task bootstrap opens the Codex SQLite index read-only and queries only identity,
archive/edge status, timestamps, `cwd`, and agent nickname. It never selects prompt-bearing
database titles, `first_user_message`, previews, logs, commands, results, or credentials. A typed
rollout reader accepts only `task_started`, assistant `agent_message`, and `task_complete`; user
messages, reasoning, tool calls, tool output, and unknown payload fields are ignored. The retained
assistant status is whitespace-normalized, secret-marker checked, and limited to 160 characters.
Whole-message JSON protocol output is discarded, and internal guardian/reviewer subagent threads
are excluded from root discovery.
Full `cwd` is reduced in memory to a hash plus final directory name before it enters PetCrew state.
Failure to read the index disables bootstrap only and never changes Codex files.

For an explicit user navigation action, PetCrew may hold the local technical Codex thread id in
memory. It is validated as a 36-character hexadecimal/hyphen identifier, never concatenated into a
shell command, and removed from snapshots before hub-cache persistence. Rust constructs the fixed
`codex://threads/` prefix and passes the resulting URI as one argument to `explorer.exe`.

OpenCode project navigation is the single opt-in full-path exception requested by the user. The
adapter places only a validated absolute session directory in `navigation.target`; `project.path`
remains null. The local latest-event registry may retain that target under its existing bounded TTL
so navigation survives a PetCrew restart. The UI never renders or logs it, and the hub removes all
navigation targets before writing `hub-cache.json`. Rust accepts only an absolute Windows directory,
percent-encodes it into the fixed `opencode://open-project?directory=...` deep link, and passes the
URI as one argument directly to the standard per-user OpenCode Desktop executable after verifying
that exact file exists. It never invokes a shell, searches PATH, edits URI-handler registry keys,
or installs an alternate executable. It cannot resume a specific OpenCode session.

Do not persist by default:

- full transcripts or prompts;
- raw shell commands;
- file contents or diffs;
- environment variables;
- access tokens, cookies, credentials, or authorization headers;
- complete tool request or response payloads.
- full project or workspace paths, except the explicit OpenCode navigation target above.

The separate versioned `settings.json` contains only allowlisted presentation values: text size,
card layout, theme, recent-result limit, and window rectangle/monitor name. It must not contain hub
tokens, Codex identifiers, task/project text, prompts, transcripts, commands, credentials, or full
filesystem paths. Invalid settings are ignored in favor of safe defaults.

## Redaction

Adapters must redact common secret patterns before transmitting an event. The event hub performs a second defensive redaction before logging or persistence.

If safe summarization is not possible, display a generic action such as `Выполняет команду` rather than leaking the raw body.

## Approval boundary

The MVP cannot approve, deny, answer, steer, stop, or resume work.

A later interactive phase requires a separate design and threat review covering:

- origin binding to the correct agent request;
- expiration and replay prevention;
- one-time versus durable permission scopes;
- high-risk command presentation;
- audit log and undo boundaries;
- protection against deceptive agent-supplied labels.

No global auto-approve control is planned.

## Failure behavior

- If PetCrew is closed, agents continue normally.
- If an adapter cannot reach PetCrew, it exits quickly and does not block ordinary work.
- Invalid events fail closed for the UI but do not interfere with the provider.
- Docking failure falls back to an independently positioned overlay.
