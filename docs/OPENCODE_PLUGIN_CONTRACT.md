# OpenCode plugin contract

## Boundary

The OpenCode adapter is an observational global local plugin. It emits PetCrew protocol `1.0`
events and cannot answer questions, approve permissions, steer sessions, stop work, modify tool
arguments, or call a model.

## Identity

- `provider`: `opencode`.
- `session_id`: opaque SHA-256 identity of the root OpenCode session.
- root `agent_id`: stable opaque identity of the root OpenCode conversation. Every new turn in that
  conversation reuses the same card.
- child `agent_id`: opaque turn identity of the child session, so helpers remain separate cards.
- `parent_agent_id`: stable root identity for a direct child, or the current opaque identity of its
  immediate child parent; `null` for the root.
- `project.id`: SHA-256 identity of the normalized project directory.
- `project.name`: final directory name only; `project.path` remains `null`.
- `navigation`: when the session has a valid absolute local directory, a provider navigation
  target with that directory and the label `Открыть проект в OpenCode`. This is the only
  full-path exception: it exists solely for the user-requested native OpenCode project jump,
  is never rendered or logged, and is removed before the hub cache is persisted.

## Allowed fields

- sanitized OpenCode session title, maximum 120 characters;
- generic action label derived only from the tool name;
- lifecycle/attention phase;
- explicit completed and total counts from `todo.updated`;
- aggregate file/addition/deletion counters from `session.diff`;
- start/update/finish timestamps;
- opaque user/assistant message ids, roles, parent linkage, and message timestamps used only to
  prove the current turn's terminal receipt;
- generic terminal result.

## Forbidden fields

Prompts, message text/parts, reasoning, file names, patches, tool input, shell commands, tool output,
error bodies, permission patterns/answers, question text/options/answers, credentials, environment
values, and OpenCode auth/config data must not enter PetCrew state, registry files, or logs. Full
paths are forbidden except for the explicit project-only navigation target defined above.

## Completion semantics

`session.idle` is terminal only after the adapter observed a completed assistant
`message.updated` whose `parentID` matches the current user message and whose timestamps belong to
the current turn. A user message followed by idle without such an assistant receipt is not
terminal. An idle historical session discovered at startup is emitted only when the official SDK
message metadata proves the same receipt after the persisted active turn's `started_at`.

The adapter never consumes message parts for this proof. It retains only the current turn's opaque
message ids and timestamps in memory. A completed terminal event uses a deterministic `event_id`
derived from raw session id, assistant message id, and terminal phase. PetCrew receives only the
resulting hashes, so replay from another plugin instance produces the same completion identity
without exposing the provider message id.

Before emitting that terminal result, the adapter requests the session's final diff through the
official SDK `session.diff` endpoint. It retains and emits only aggregate file/addition/deletion
counters. Failure or malformed data never blocks completion; the last valid pushed aggregate is
kept when available.

A completed root card returns to the state of a newer turn when an event carries a valid
`started_at` later than the card's previous terminal `updated_at`. This normally happens on explicit
`agent.started`; it also permits the latest-state registry to recover from `activity`, attention, or
completion when `agent.started` was overwritten while PetCrew was closed. Starting that next turn
clears the previous unread result because the user has continued the conversation. An event from the
old turn cannot reopen the card. The hub canonicalizes legacy turn-scoped OpenCode root identities
and keeps only the newest state for each root conversation, so upgrading does not require deleting
the local cache.

## Existing work

On plugin startup, the adapter may call the official SDK's session list, status, and bounded
message-list endpoints. From the message list it inspects only `info.id`, `info.sessionID`,
`info.role`, `info.parentID`, and `info.time`; parts are ignored. It
caches session identity/title/parent metadata and emits only sessions currently reported as busy.
The plugin must register and return all live hooks before scheduling this recovery; awaiting an SDK
request inside plugin initialization can deadlock the desktop-owned local server. It does not query
messages, parts, files, or the local database. Diff retrieval is limited to the single documented
aggregate source at live-turn completion and discards names and patch bodies.

Startup recovery also repairs a missed CLI completion without broad historical replay. For each
bounded recent root session, the adapter computes its existing opaque identity and reads only that
PetCrew-owned latest-event registry file. If the record is fresh, exactly matches the opaque root,
has phase `working`, the official session status is `idle` or absent, and bounded official message
metadata proves a completed assistant response for that turn, the adapter restores the sanitized
`started_at` and emits one generic completion through the ordinary transport. Terminal,
attention, stale, malformed, child, or unrelated records remain untouched. No message or result is
persisted or logged to make this decision.

## Transport

The plugin uses the existing authenticated PetCrew loopback protocol and PetCrew-owned atomic
latest-state registry. It has no third-party dependency. All failures are best-effort, bounded, and
non-blocking for OpenCode.

## Completion consumer handoff

The PetCrew hub records every accepted OpenCode terminal event in its separate sanitary completion
inbox. The adapter remains the only OpenCode event observer; no second SSE watcher is added.

Consumers correlate sessions with the existing opaque identity:

```text
session_id = "session:" + sha256(trim(raw OpenCode session id), UTF-8, lowercase hex)
```

They read `GET /v1/completions?after=<cursor>` on a later invocation or hold
`GET /v1/completions/stream?after=<cursor>` during a current invocation, using the endpoint and
secret discovered through the existing `hub-runtime.json` / `secret_file` contract. After matching
a terminal record, the consumer reads the full final response directly from its own authenticated
OpenCode API. PetCrew does not store or return that response.

The consumer persists the greatest processed cursor. It must tolerate duplicate delivery by
`completion_id`, handle `completed`, `failed`, and `cancelled`, and perform explicit provider
recovery if the inbox reports `truncated: true`. Completion-feed reads do not acknowledge PetCrew
cards.
