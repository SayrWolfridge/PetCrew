# Architecture

## System boundary

PetCrew is a separate local Windows application. It does not patch Codex, replace the installed pet, or depend on private application binaries.

```text
┌──────────────────┐
│ Codex adapter    │──────┐
└──────────────────┘      │ normalized sanitary events
                          ▼
┌──────────────────┐   ┌──────────────────────────────┐
│ OpenCode adapter │──▶│ PetCrew Core                 │
└──────────────────┘   │ validation + recovery reader │
                       │ durable sanitary state        │
                       │ snapshot + SSE + completion   │
                       └──────────────┬───────────────┘
                                      │ authenticated loopback
                              ┌───────▼────────┐
                              │ PetCrew Monitor│
                              │ Tauri + React  │
                              └────────────────┘
```

PetCrew Core is the single owner of provider observation and normalized state. The monitor is a
replaceable authenticated client: closing or rebuilding the window must not reset a live turn or
start a second provider watcher. During the source migration the existing desktop binary may host
the same Core runtime in-process, but the transport contract is identical to the standalone
headless Core. Only one Core may own the runtime descriptor and recovery loop at a time.

## Components

### Overlay UI

Responsibilities:

- render the pet anchor and a dynamic agent list;
- choose detailed, compact, or grouped density;
- display Russian semantic summaries;
- preserve unread completions;
- provide local acknowledgement and navigation actions;
- expose manual list/tile layout, dark/light theme, and three text sizes independently of density;
- keep the list surface mounted during history-filter and acknowledgement changes so its scroll
  position survives local updates;
- provide an independently scrolling list and bounded native window resizing.

The UI consumes provider-neutral snapshots only. It must not parse Codex or OpenCode payloads.

The borderless shell grants only the extra native window permissions it uses: close, drag,
read/set size, and start southeast resize dragging. The configured logical size is bounded from
390x620 to 900x1000. Agent cards never shrink to fake fitting; overflow belongs to the list, which
supports wheel input and a persistent WebView scrollbar.

The root document and shell are explicitly bound to the current viewport height. Header, overview,
and footer are non-shrinking; the team section owns the remaining height with `min-height: 0` and
hidden outer overflow. This constraint is required for the inner agent list to receive a real scroll
range instead of growing below a clipped body. On startup the shell reapplies its current logical
window size once, which initializes native borderless resize hit-testing before the first user drag.

### PetCrew Core

Responsibilities:

- accept local adapter events;
- authenticate event writers;
- validate the JSON contract;
- enforce monotonically increasing sequence numbers per agent session;
- normalize time, state, and attention;
- maintain an in-memory snapshot and a minimal local cache;
- publish complete snapshots and revisioned snapshot events;
- remain independent of monitor-window lifetime.

Transport is authenticated loopback HTTP. Short-lived reporters submit events; monitors read a
complete snapshot and a revisioned SSE feed. The internal Tauri event remains a migration
compatibility channel only and is not the long-term source of truth. WebSocket or named pipes may
be introduced only if measurement shows a need.

The desktop runtime performs the same bounded registry and Codex-index import immediately on a
Tauri `RunEvent::Resumed`. This explicit resume refresh complements the five-second poll because
Windows may suspend timer delivery while the machine sleeps.

### Local presentation settings

The native core owns a versioned `settings.json` in the Tauri local application-data directory.
Rust validates a strict allowlist of presentation fields and writes updates through a same-directory
temporary file with rollback-safe replacement. Preference updates and window-placement updates are
separate commands so a delayed geometry event cannot overwrite a newer theme, layout, or text size.

The file stores only `text_size`, `card_layout`, `theme`, `recent_completed_limit`, and an optional
window rectangle plus monitor name. On startup the UI restores a saved rectangle only when it still
intersects an available monitor work area; otherwise it uses the secondary-monitor placement rule.
Move and resize events are debounced before persistence.

The v1 runtime contract is deliberately small:

- bind an ephemeral port on `127.0.0.1` only;
- publish endpoint metadata in `hub-runtime.json` under the Tauri local application-data directory;
- keep the per-install bearer secret in a separate `hub-secret.txt` file in the same user-owned directory;
- expose unauthenticated `GET /health` with generic health metadata only;
- accept authenticated `POST /v1/events` with a 64 KiB body limit and a two-second request deadline;
- expose authenticated read-only OpenCode completion inbox and SSE endpoints from the same hub;
- expose authenticated `GET /v1/snapshot` and `GET /v1/snapshots/stream?after=<revision>`;
- accept authenticated local acknowledgement without involving a provider;
- emit the complete normalized snapshot to the main window as `petcrew://snapshot` after every accepted change;
- expose initial snapshot, local acknowledgement, and demo reset through Tauri commands available only to the bundled main window.

For user-global recovery across projects, sanitized hooks also atomically replace one latest-event
record per opaque agent under `agent-registry/` in the same Tauri local-data directory. This is a
minimal state registry, not a transcript or event log. The hub imports fresh records at startup and
every five seconds, so PetCrew can reopen after being closed without model calls or private Codex
inspection. Active records expire after 24 hours without updates; terminal registry files expire
after seven days. Clearing the hub also clears this PetCrew-owned registry.

Tasks that already existed before hooks were loaded are bootstrapped directly by the desktop hub
from the local Codex state index in read-only mode. The hub selects only recent non-archived root
threads and recent open child edges, joins safe display names from `session_index.jsonl`, converts
full paths to final directory names in memory, and emits recovered placeholders. Authoritative hook
events supersede matching placeholders. A missing, locked, or incompatible index disables only this
bootstrap lane.

The bootstrap may attach a transient technical thread id as a navigation target. The UI sends only
that id to a Tauri command; Rust validates the UUID-like value, constructs the canonical
`codex://threads/<thread-id>` URI, and asks Windows Explorer to open the registered Codex handler.
OpenCode navigation uses the same explicit user-action boundary: the adapter supplies its current
absolute session directory only as a provider navigation capability, and Rust validates and
percent-encodes it into `opencode://open-project?directory=...`. OpenCode currently opens the
project, not a guaranteed prior session. On Windows, PetCrew passes that URI directly to the
verified standard per-user OpenCode Desktop executable. It does not rely on a registered Windows
URI handler because the installed application may parse deep-link arguments without registering
the `opencode` protocol.
Navigation targets are stripped before hub-cache persistence.

Accepted OpenCode terminal events also append a minimal completion record to the hub cache. This
separate journal is required because the provider-neutral latest card state may legitimately move
into a newer turn before an external consumer reads the prior completion. The journal contains only
opaque identity, terminal phase, timestamp, and a monotonic cursor; it never contains card text,
project metadata, paths, or provider content. Authenticated inbox and SSE reads reuse the existing
loopback listener and secret. SSE listens to the same accepted-event notification path and does not
observe OpenCode itself.

The bundled demo writer uses the same authenticated HTTP endpoint as future adapters. This proves the transport without installing Codex or OpenCode hooks.

Demo event identity includes the agent's accepted next `sequence`, not only an in-memory run/step
counter. After an application restart, a restored snapshot therefore continues with a new event ID
instead of replaying `restored:step:1`. Rejected demo events expose the hub error code in the UI.

### Codex adapter

The supported first adapter is a local Codex plugin with two lanes:

- sanitized lifecycle hooks for root task lifecycle, generic activity, attention, completion, and best-effort child lifecycle;
- an optional local MCP semantic reporter for stable child identity, truthful plans, readable actions, and completion summaries.

The adapter writes the latest sanitized state to the global registry, attempts the authenticated
loopback event, and exits. It does not return approval decisions, rewrite tool input, or block agent
execution. The installed personal plugin applies across Codex projects; the repository is only its
development source.

Hooks alone are insufficient for exact per-subagent activity because current tool-lifecycle hook payloads do not reliably attribute every event to a unique child. PetCrew never creates child cards by guessing from names or timing.

Codex App Server exposes richer thread, turn, plan, item, and collaboration events. It is the preferred future deep-integration protocol, but the current desktop-owned App Server uses a private stdio channel and exposes no supported external observer endpoint. PetCrew does not start a parallel Codex runtime or scrape private desktop traffic. See `docs/CODEX_ADAPTER_AUDIT.md`.

### OpenCode adapter

The second adapter is a dependency-free global local OpenCode plugin. It maps official session,
permission, question, todo, and tool lifecycle events to the same contract. The plugin uses the
official SDK only for bounded startup discovery of session metadata and current busy state; it does
not read messages or the local database. A root conversation keeps one stable card across turns;
child sessions remain distinct through opaque
opaque identities and `parent_agent_id`. No UI fork is permitted for provider-specific behavior.

The same adapter terminal event is the sole input to the OpenCode completion inbox. A consumer may
use that sanitary signal to decide when to call its own authenticated OpenCode API for final text;
PetCrew does not make or proxy that call.

The bounded startup pass may also close one missed CLI turn from PetCrew's own latest-state
registry: a fresh matching root `working` record plus official `idle` status produces the ordinary
generic terminal event. This is restart reconciliation, not polling or historical replay. It never
reads provider messages and does not act on attention, terminal, child, stale, or malformed records.

See `docs/OPENCODE_ADAPTER_AUDIT.md` and `docs/OPENCODE_PLUGIN_CONTRACT.md`.

### Semantic status reporter

Raw hooks can say that an agent invoked a tool, but they cannot reliably say `step 4 of 10` or produce a short business-level explanation.

Agents therefore use an explicit local MCP reporting operation bundled with the Codex plugin:

```text
start(total=10, title="Audit Codex integration")
step(current=4, label="Check PermissionRequest events")
complete(summary="Hook contract verified")
```

The reporter may later be exposed as a local MCP tool, provider plugin helper, or command. The event contract stays unchanged.

## State model

The store is keyed by `provider + session_id + agent_id`. A parent task may have many agent records.

Each incoming event has a per-agent `sequence` number. Stale or duplicate events cannot roll the displayed state backward.

Completion is sticky: a later process-discovery or activity event from the same turn cannot turn a
completed agent back into working. An OpenCode root event with a valid `started_at` later than the
stored terminal time reactivates the stable conversation card for its next turn. This timestamp
proof is required because the latest-state registry may overwrite `agent.started` while PetCrew is
closed.

The store normally retains up to 100 agent records. At that boundary it evicts only the oldest
acknowledged terminal records. Non-terminal and unread terminal records are protected; if those
records alone exceed 100, the snapshot includes an explicit overflow count and keeps every
protected record.

The UI independently applies the user preference for acknowledged history (ten newest results by
default). It always renders all protected records, then adds the configured recent tail. Filter and
acknowledgement updates keep the existing list DOM mounted so its `scrollTop` is preserved. This
policy is provider-neutral and will apply unchanged to Codex and OpenCode adapters.

## Docking strategy

Docking is deliberately progressive:

1. Independent always-on-top overlay with saved position.
2. User-controlled visual placement beside the Codex pet.
3. Read-only Win32 discovery spike for a stable pet window handle.
4. Automatic docking only if the handle and geometry remain stable across app restarts and updates.

Failure to discover the host pet never prevents PetCrew from operating independently.

## Technology choice

Validated frontend stack:

- React and TypeScript for the adaptable UI;
- Vite for development and production bundling;
- Vitest for model-level contract tests;
- fixture-driven tests before live adapters.

Validated native stack:

- Tauri 2 for a lightweight transparent Windows window and internal UI events;
- Rust for loopback transport, normalized state, validation, persistence, and Win32 integration;
- JSON Schema for the provider boundary;
- Microsoft MSVC C++ Build Tools for Windows linking.

Why not start from the reviewed prototypes:

- their provider claims and release maturity are inconsistent;
- their interfaces assume a small activity list;
- PetCrew requires truthful semantic progress and a provider-neutral contract from the start.

Reusable ideas may be studied, but project code must have clear provenance and license compatibility.

## Repository modules

```text
apps/overlay/          React Monitor and Tauri/Rust Core
  src/components.tsx   presentational agent cards, progress, and pet shell
  src/App.tsx          UI state, provider connection, settings, and orchestration
  src-tauri/src/
    core_ownership.rs  one-live-Core lock and stale-process recovery
    hub.rs             event store, recovery readers, HTTP/SSE, and Tauri bridge
    settings.rs        validated local presentation settings
adapters/opencode/     dependency-free OpenCode plugin adapter
plugins/petcrew/       Codex hooks, bridge, reporter skill, and tests
shared/schemas/        provider-neutral contracts
tests/fixtures/        deterministic UI and state fixtures
docs/                  product and architecture truth
tools/                 deterministic repository verification
```

Monitor and Core intentionally share one Rust crate while their executable
entry points remain separate. Provider adapters remain outside that crate and
communicate only through the versioned event contract. See
`REPOSITORY_BOUNDARIES.md` for the public-source and local-operations split.
