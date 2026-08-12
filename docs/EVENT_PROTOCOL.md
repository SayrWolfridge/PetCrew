# Event protocol

## Goals

- Represent Codex and OpenCode agents identically in the UI.
- Preserve truthful progress and readable outcomes.
- Support one to many agents without provider-specific UI code.
- Allow the protocol to evolve without silently breaking old adapters.

## Envelope

Every event contains:

```json
{
  "protocol_version": "1.0",
  "event_id": "unique-event-id",
  "sequence": 12,
  "occurred_at": "2026-07-17T16:00:00+03:00",
  "provider": "codex",
  "session_id": "session-id",
  "agent_id": "agent-id",
  "parent_agent_id": "root-agent-id",
  "event_type": "agent.progress",
  "payload": {}
}
```

## Event types

- `agent.discovered`
- `agent.started`
- `agent.progress`
- `agent.activity`
- `agent.attention_requested`
- `agent.attention_resolved`
- `agent.completed`
- `agent.failed`
- `agent.cancelled`
- `agent.acknowledged`

## Normalized snapshot fields

The state store produces a snapshot with:

- identity: provider, session, agent, and parent agent;
- project: stable id, display name, and optional local path;
- task: concise title and optional detail;
- phase;
- progress;
- optional provider-reported change summary;
- current semantic action;
- optional current-turn `started_at` timestamp;
- attention type and summary;
- completion result;
- navigation target;
- timestamps and unread state.

## Progress contract

```json
{
  "kind": "steps",
  "current": 4,
  "total": 10,
  "label": "Проверяет события PermissionRequest",
  "source": "explicit"
}
```

Rules:

- `steps` requires integers with `0 <= current <= total` and `total > 0`.
- Only `source: explicit` may render a fraction or percentage.
- `source: inferred` may update a label or phase but not `current` or `total`.
- If a declared plan changes, the agent must issue a new explicit progress event with the revised total.
- The UI labels revised plans honestly; it does not pretend the denominator was stable.

## Change summary

```json
{
  "files": 2,
  "additions": 164,
  "deletions": 0,
  "source": "provider"
}
```

`change_summary` is optional turn-scoped technical telemetry. It is not semantic task progress and
must not fill or relabel the progress bar. All three counters are non-negative integers reported by
the provider's supported event surface. Adapters may aggregate structured provider diff records but
must not transmit file names, patches, tool bodies, prompts, or command output. If a provider does
not expose a complete trustworthy aggregate through the approved adapter surface, the field is
omitted rather than reconstructed from the working tree.

## Semantic summaries

`current_action` and `result.summary` are short user-facing strings, preferably Russian. Raw tool names may be kept as secondary metadata.

`started_at` is an optional RFC 3339 timestamp for the current uninterrupted turn. `agent.started`
defaults it to `occurred_at`; subsequent activity preserves it. Recovered adapters may provide the
typed lifecycle timestamp. The UI omits elapsed time when no valid start timestamp exists.

Recommended limits:

- task title: 120 characters;
- current action: 160 characters;
- result summary: 500 characters;
- technical detail: expandable and never required for the compact row.

## Attention

```json
{
  "kind": "input",
  "summary": "Нужно выбрать один из трёх вариантов",
  "requested_at": "2026-07-17T16:02:00+03:00"
}
```

Kinds:

- `input`
- `approval`
- `blocked`
- `failure`

The read-only MVP displays attention but cannot resolve it externally.

## Completion

```json
{
  "summary": "Проверка завершена: формат hooks подтверждён",
  "outcome": "success",
  "completed_at": "2026-07-17T16:08:00+03:00",
  "unread": true
}
```

Unread completion remains visible and protected from retention eviction until a local
acknowledgement event. Acknowledged terminal records join the recent-results tail: the interface
shows the newest user-configured number (ten by default), while the store evicts the oldest
acknowledged terminal records first at its normal capacity of 100. Non-terminal and unread records
are never removed to enforce that capacity; protected overflow is reported explicitly.

## Navigation target

Navigation is optional and capability-based:

```json
{
  "kind": "terminal",
  "label": "Открыть терминал",
  "target": "opaque-provider-value"
}
```

The UI must hide unsupported navigation actions rather than fabricate a jump target.

Provider navigation may carry a validated local project directory when the provider's supported
desktop deep link requires it. For OpenCode, `kind: "provider"` means “open this project in
OpenCode”; it does not claim to resume the exact session. The target is local-only capability data,
must never be displayed or logged, and is stripped from the durable hub cache.

## Compatibility

- Additive optional fields are allowed within protocol `1.x`.
- Removing or changing meaning requires a major version.
- Unknown event types are logged as metadata and ignored safely.
- Invalid events never update visible state.

## Loopback HTTP transport v1

PetCrew selects an ephemeral port and binds only to `127.0.0.1`.

### Discovery

The Tauri local application-data directory contains:

- `hub-runtime.json`: endpoint, protocol version, process id, and the path of the secret file;
- `hub-secret.txt`: the per-install bearer token.

Writers must treat a runtime descriptor as stale until `GET /health` succeeds quickly. The descriptor never contains the token itself.

### Health

`GET /health` does not require authentication and returns only:

```json
{
  "status": "ok",
  "protocol_version": "1.0"
}
```

### Submit event

`POST /v1/events` requires:

```text
Authorization: Bearer <per-install-secret>
Content-Type: application/json
```

The body is one event envelope from this document. Success returns HTTP `202` with the new snapshot revision. Expected failures:

- `400`: malformed or semantically invalid event;
- `401`: missing or incorrect bearer token;
- `409`: duplicate `event_id`, stale sequence, or update after a terminal state;
- `413`: body exceeds 64 KiB;
- `408`: request exceeds two seconds.

Error responses contain a short stable code and never echo the submitted body or credential.

## Monitor snapshot channel v1

The authoritative monitor transport is the same authenticated loopback Core endpoint used by
adapters:

- `GET /v1/snapshot` returns the current complete snapshot;
- `GET /v1/snapshots/stream?after=<revision>` returns SSE event `snapshot` whenever the revision is
  greater than `after`; the current snapshot is sent immediately when applicable;
- `POST /v1/acknowledgements` with `{"key":"<normalized-card-key>"}` marks one terminal result read
  and returns the new complete snapshot.

All three require the existing bearer token. The stream `id` is the decimal snapshot revision.
Keepalives carry no state. A reconnecting monitor first reads `/v1/snapshot`, then resumes the
stream from that revision; it never reconstructs state by merging missed provider events.

Acknowledgement is local PetCrew state only. It cannot answer a provider question, approve a
command, resume a task, or change provider data.

During migration the bundled monitor may also receive the same complete payload over the internal
Tauri event:

After an accepted change, Rust emits `petcrew://snapshot` with:

```json
{
  "revision": 12,
  "agents": [
    {
      "key": "codex:session-id:agent-id",
      "provider": "codex",
      "session_id": "session-id",
      "agent_id": "agent-id",
      "parent_agent_id": null,
      "project": "PetCrew",
      "task": "Проверить local hub",
      "phase": "working",
      "progress": {
        "kind": "steps",
        "current": 4,
        "total": 10,
        "source": "explicit"
      },
      "current_action": "Проверяет приём событий",
      "result": null,
      "unread": false,
      "last_sequence": 12,
      "updated_at": "2026-07-17T16:00:00+03:00"
    }
  ]
}
```

The UI replaces its live view with the whole snapshot. It does not merge provider payloads or infer missing progress.

## OpenCode completion inbox v1

The existing OpenCode adapter and hub also provide a read-only completion signal for local
consumers that already know the OpenCode session they started. This is not a second OpenCode
observer and does not contain the final assistant message.

Only accepted OpenCode `agent.completed`, `agent.failed`, and `agent.cancelled` events create
completion records. For `agent.completed`, the adapter event id is a deterministic hash receipt
anchored to the completed assistant message; Core hashes that event id into `completion_id`.
Repeated delivery of the same assistant terminal therefore keeps one durable completion record:

```json
{
  "cursor": 42,
  "completion_id": "completion:<sha256>",
  "provider": "opencode",
  "session_id": "session:<sha256>",
  "agent_id": "root:<sha256>",
  "parent_agent_id": null,
  "phase": "completed",
  "completed_at": "2026-07-26T12:00:00+03:00"
}
```

The record deliberately excludes task/project labels, paths, result summaries, progress, change
telemetry, prompts, messages, tool data, and OpenCode API payloads. `session_id` and agent
identities are the same opaque identities already used by the OpenCode adapter.

### Sticky inbox

`GET /v1/completions?after=<cursor>` requires the existing bearer token and returns:

```json
{
  "protocol_version": "1.0",
  "oldest_cursor": 40,
  "latest_cursor": 42,
  "truncated": false,
  "completions": []
}
```

`completions` contains retained records whose cursor is greater than `after`. Omit `after` to read
all retained records. The hub retains at most 512 records for seven days, independently of card
unread/acknowledgement state and independently of a newer turn on the same card. `truncated: true`
means the requested cursor predates retained history; the consumer must recover explicitly from
its authoritative provider.

### Live stream

`GET /v1/completions/stream?after=<cursor>` requires the same bearer token and returns
`text/event-stream`. Every record is sent as event `completion`, with the decimal cursor as the SSE
`id` and the record as JSON `data`. The stream first emits retained records after the supplied
cursor, then waits for new records from the same accepted-event path. Keepalives contain no state.

The stream is only a current-connection convenience. A closed Codex turn cannot be awakened by MCP
alone, so consumers must use the sticky inbox on their next invocation. Reading either endpoint is
read-only and never acknowledges a PetCrew card.
