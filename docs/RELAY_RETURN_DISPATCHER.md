# Relay return dispatcher contract

Status: production contract; Monitor recovery boundary updated 2026-09-27.

## Outcome

The existing PetCrew Relay remains the only completion consumer and the only component that starts
`codex exec resume`. OpenCode and Codex return receipts enter one durable dispatcher keyed by the
source Codex task. Different sources may run in parallel; one source may have only one Relay-started
Codex turn at a time.

For delegated Codex tasks, the dispatcher preserves the existing autonomous processing contract.
For OpenCode returns whose parent is a Windows Codex Desktop task, D-091 is also enforced in the
runtime: the exact receipt is claimed, persisted and published as ready in Monitor, but Relay does
not start a standalone Codex App Server status probe or `codex exec resume`. Those processes cannot
share Desktop's writer and were adding avoidable work to the same App Server/state subsystem during
observed queue stalls. Lisa's next message in the already-open source task is the supported pickup
event; Codex then reads the exact stored receipt without resending OpenCode work.

No timer, heartbeat, model poll, second Relay, recurring job, transcript write or automatic retry is
part of the pickup path. A future supported same-owner Desktop queue endpoint may replace this
policy after a separate production gate.

## Monitor recovery boundary

Relay recovery remains an explicit user action. The Codex reader binds only to `127.0.0.1:4099`
and exposes unauthenticated `GET /health` solely for generic readiness metadata. The response is
HTTP 200 JSON with `service=codex-return-reader`, `protocol_version=1`, `relay_ready`,
`opencode_ready`, and overall `healthy`; it contains no secret, path, task identity, provider
content, or process details. Every Codex anchor/delta operation remains protected by the existing
Basic-auth secret. The reader probes the authenticated OpenCode `/global/health` endpoint itself,
so Monitor never receives or reconstructs `OPENCODE_BRIDGE_PASSWORD`.

Monitor treats a listener as ready only when the HTTP status and the complete health contract
match. A TCP connection, an arbitrary `HTTP/` response, an authentication error, malformed JSON,
or a response from a different service is unhealthy.

Before stopping the scheduled task, Monitor captures the exact listener-owner identity in memory:
PID, image name, executable path, command line, and process creation time. The Relay owner must be
`pythonw.exe` running `opencode_external_server.py`; the OpenCode owner must be `opencode.exe`
running `serve --hostname 127.0.0.1 --port 4098`. If a process remains after the normal scheduled-
task stop, Monitor queries it again and permits `taskkill` only when the full identity is unchanged
and still matches the expected role. Missing, inaccessible, changed, or ambiguous identity fails
closed without killing the process.

## Codex parent-only transfer

Changing which source Codex task owns the Relay relationship for a delegated Codex target is a
separate local metadata operation from preparing or confirming a return. The MCP surface exposes
`codex_get_return_parent` for inspection and `codex_transfer_return_parent` for the explicit
change. The transfer request contains exact `target_thread_id`, `expected_binding_id`,
`new_source_thread_id`, `new_workspace`, and `transfer_parent: true`. `binding_id` is the binding
generation and compare-and-swap token. All inputs are validated before the existing
`codex-bindings/<sha256(target)>.json` record is replaced.

`new_workspace` may differ from the previous source parent's workspace. It must be an existing
absolute directory, but transfer performs no reads or writes in either project beyond validating
the directory and atomically replacing Relay metadata. A real transfer creates a fresh
`binding_id`, retains receipt anchors, attempted/delivered evidence and cursor bookkeeping, and
writes `return_state: parent_only`. The response reports previous and new parent/workspace, old and
new generation, `return_state: parent_only`, `return_armed: false`, and `binding_changed: true`.
Parenthood alone therefore cannot arm or emit a return.

The operation rejects target=self, a missing or stale expected generation, an invalid path, a
missing explicit transfer flag, and malformed identifiers before mutation. A retry naming the
already-current parent is idempotent only when expected generation and workspace also match; it
returns the existing binding byte-identically and preserves an armed route. A same-parent request
cannot change workspace.

The binding update reuses `_binding_lock_for_opaque`, extended with a standard-library OS
process-shared lock while retaining thread reentrancy and deterministic lock ordering. Every Codex
binding writer that performs a read/check/write transition participates, including register,
confirm, rollback, detach, receipt claim, and delivered bookkeeping where applicable. Atomic
temporary-file replacement prevents torn files; the process-shared critical section makes the
expected-generation check and replacement one CAS operation across Bridge processes. Tests use two
separate Python processes to prove one stale-CAS loser, serialized writes, and lock release after
process exit. If replacement fails, the prior valid relationship and evidence remain authoritative;
no second parent store is introduced. Existing Codex legacy migration semantics remain unchanged;
no installed or live binding file is migrated automatically.

Provider admission and pre-launch validation continue to require the exact binding generation,
source parent, target and workspace. A queued record from the old generation therefore fails closed
after transfer and cannot be redirected to the new parent. A return whose process already started
cannot be undone; its receipt is neither replayed nor deleted. This limitation is reported by the
transfer result.

The normal `codex_bind_return` -> send -> `codex_confirm_return` workflow may prepare and arm a new
one-shot return from `parent_only` state. That preparation creates its own fresh generation and
uses the existing rollback and delivery rules. Parent transfer never captures a target anchor,
calls app-server/`thread/resume`, queries the PetCrew inbox, sends a prompt, or makes a network
request. It does not change Desktop thread ancestry, repair the reader anchor, or prove Desktop
idle auto-drain.

## Durable return state

One write-ahead return record is created before provider receipt state is mutated. Its immutable
identity is the provider plus exact completion receipt and binding generation. The record contains
only technical routing fields already allowed by the binding contract, timestamps, safe outcome
classes, and bounded turn evidence. It never stores a prompt, assistant response, reasoning, tool
body, command body, credential, environment, stdout, or stderr.

Lifecycle states are:

`received -> waiting | launching -> running -> finished`

and terminal problem states `failed` and `uncertain`.

- `received`: the exact receipt and current binding were durably reconciled.
- `waiting`: the source is authoritatively active, or status is temporarily unavailable. The
  receipt remains eligible for event-driven recovery.
- `launching`: the source was confirmed idle immediately before a CLI attempt, but no structured
  Codex turn-start event has been observed yet.
- `running`: structured CLI output proved the exact Relay-started turn began. Persist its bounded
  technical turn id and no model text.
- `finished`: the structured turn ended and the CLI exited successfully. This means Relay handling
  ended; it does not claim that the maker artifact passed semantic review.
  For an OpenCode Desktop pickup, `terminal_evidence=monitor_pickup_ready` also closes only the
  dispatcher job: the provider receipt remains exact and readable, and no in-chat delivery is
  claimed.
- `failed`: the attempt is proven not to have started, or ended with a classified failure. It is
  visible and is not automatically retried unless a supported check proves the failure was an
  unstarted busy/transient collision.
- `uncertain`: a turn may have started but its terminal result cannot be reconciled. It is visible
  and never causes blind re-execution.

The journal keeps at most 512 return records globally and 64 recent receipt summaries per source.
Terminal retention removes only records whose provider delivery bookkeeping is already closed.

## Atomicity and recovery

Receipt intake is a recoverable two-phase transaction under one process-global journal lock:

1. validate the current provider binding and atomically persist the write-ahead `received` record;
2. atomically claim the exact receipt in that provider binding;
3. atomically mark the journal record claimed.

A crash between steps is reconciled from the write-ahead record and exact binding generation. A
simultaneous duplicate sees the same durable job id and cannot create a second runner call. The
single scheduled Bridge ownership/startup guard remains the process-level exclusion; the journal
does not claim to coordinate an unsupported second Relay process.

On startup or PetCrew reconnect, Relay replays only status outbox events and reconciles durable
records. It must not launch `received`/`waiting` work merely because the transport reconnected or
the Bridge process restarted: reconnect is not evidence that the source writer became available,
and a historical retained receipt may already have been accepted or superseded outside an older
dispatcher version. Such work stays visible and durable until a new authoritative terminal event
from that exact source Codex task requests the event-driven drain. A record left `running` or
`launching` across process death is not rerun without exact evidence that no Codex turn started.
Otherwise it becomes `uncertain`.

Binding detach/transfer is checked again immediately before launch. A mismatched generation closes
the record as stale/failed without waking the former owner.

## Busy and Desktop race contract

Relay uses the non-model Codex app-server observer methods `thread/read` with
`includeTurns: false`, followed by bounded, paginated `thread/turns/list`, to inspect the source
without claiming a writer. The reader validates the exact task id and continues to omit private
reasoning from returned payloads. Capability clarification (verified 2026-09-06 against the
installed CLI 0.153.4 schema and official app-server documentation): these methods read stored
task metadata and append-only turn history without `thread/resume`.

The returned status is a persisted app-server snapshot. An idle or terminal snapshot does not
prove that Codex Desktop is available for a new writer, and a separate app-server may report a
Desktop-owned task as `notLoaded`. Reader errors and ambiguous states therefore remain unknown and
fail closed into durable waiting/reconciliation.
Only an explicit terminal/idle state permits launch. Active state produces `waiting`; unavailable
or unknown state fails closed into durable waiting/reconciliation.

Relay rechecks under the shared per-source lock immediately before spawning the CLI and rechecks
after enqueue, closing the terminal-before-enqueue race. The CLI is invoked with structured JSONL
events. Those events are streamed and discarded; Relay retains only whether the turn started,
its bounded technical id, terminal evidence, exit code, and a safe error class.
The documented JSONL `turn.started` can omit `turn_id`; retain null in that case.
Correlate start/end to this one CLI invocation and durable job id, never manufacture
a Codex turn id or reconcile an unknown turn by selecting the latest history entry.

A local lock cannot exclude a Desktop turn that starts after the final status check. Therefore:

- nonzero CLI exit alone is never labelled `busy`;
- an exact source-qualified writer-ownership refusal, observed in bounded structured output or
  bounded stderr while stderr is still fully drained, with no structured start preserves the job
  in `waiting` for a later event-driven drain; a terminal persisted snapshot does not negate
  ownership, and this refusal must not be labelled active model work;
- structured start plus later failure becomes `failed` or `uncertain` and is never rerun blindly;
- a source turn begun by the user while Relay is already running cannot start a second Relay job;
  newer user instructions remain in the task history and take precedence during the resumed review.

Live Desktop concurrency remains a deployment canary. Source tests may prove the state machine and
runner evidence contract, but cannot claim global Desktop exclusion.

The production OpenCode pickup path deliberately bypasses this observer and runner entirely. The
observer remains available for Codex-to-Codex return handling and explicit read operations; it is
not started merely because an OpenCode model completed.

## PetCrew presentation contract

Execution and presentation are independent. Every state transition first persists one sanitized
outbox event. Publication failure retries only the outbox on the existing reconnect path and never
restarts a model or changes receipt execution state. HTTP `202` and deterministic replay `409` are
publication success.

There is one stable status card per source Codex task:

- `provider=codex`;
- `session_id=session:<sha256(source task id)>`;
- `agent_id=relay:<sha256(source task id)>`;
- `event_type=agent.discovered` so presentation never enters the completion journal;
- a per-source durable monotonic sequence across both providers;
- exact Codex task navigation and no result text.

Visible mappings are:

- `received`: `queued`, `Возврат получен`;
- `waiting`: `queued`, `Результат сохранён, ожидает передачи в Codex`;
  when a structured writer refusal proves that no turn started, the status is
  `Codex пока не принимает возврат; результат сохранён`. Waiting alone is not
  evidence that a model turn is active: an idle Desktop may retain writer ownership.
- `launching`: `queued`, `Запускает обработку возврата`;
- `running`: `working`, `Codex принимает результат <provider>`;
- `finished`: `completed`, unread result `Обработка возврата завершена — открыть задачу`;
- OpenCode pickup-ready: `completed`, unread result
  `Результат OpenCode сохранён — напишите задаче забрать его`;
- `failed`: `failed`, unread safe failure summary;
- `uncertain`: `failed`, unread `Обработка могла начаться; автоматический повтор остановлен`.

Core may reactivate a terminal card only for the exact authenticated `relay:<64 hex>` identity and
only when a newer `started_at` proves a new return run. This narrow exception does not change normal
Codex recovery cards, result acknowledgement, provider agents, or completion receipt production.

Historical `result-ready` outbox files remain replayable during migration. New returns use the
stable status card and do not create a second per-receipt result card.

Implementation clarification: journal state and its pending sanitized status envelope
are persisted in one atomic write. The envelope is then staged to the independent
status outbox before removing it from the journal. A crash at either boundary leaves
an idempotently replayable envelope; publication never invokes execution.
Capacity backpressures new intake when only active/unclosed records remain. Completion
stream cursor must not advance past an intake exception. Provider generation remains
immutable; claimed and bookkeeping_closed are explicit fields, not inferred from it.

## Acceptance

Offline deterministic tests must cover:

1. idle return: one structured start, running event, and finished event;
2. active source: no runner, durable waiting, source-terminal automatic drain;
3. terminal-before-enqueue and post-check Desktop-start races;
4. two OpenCode bindings and mixed OpenCode/Codex returns serialized for one source while another
   source remains independent;
5. simultaneous duplicate receipt and write-ahead/binding crash reconciliation;
6. restart/reconnect/repeated busy without loss or duplicate execution;
7. exit before start versus after-start uncertain/failure behavior;
8. outbox replay without a runner call or completion feedback loop;
9. detach/transfer generation mismatch and stale-source prevention;
10. the existing exact-read, persistent-binding, result-ready migration, startup guard, and model
    disclosure tests remain green.

The production gate additionally requires an isolated candidate diff, candidate/source/installed
hash map, exact backup and rollback, then live idle/active/Desktop-race canaries. Monitor/Core live
installation must be coordinated with the guardian candidate and receive one combined constrained
`npm run tauri:build` (`BelowNormal`, `CARGO_BUILD_JOBS=2`) at the accepted batch boundary.
