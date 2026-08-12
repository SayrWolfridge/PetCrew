# Read-only MVP

## Objective

Prove that PetCrew can render and maintain a truthful, useful view of a dynamic team before touching live Codex configuration.

## Slice 0 - architecture scaffold

- Project card and local rules.
- Product and architecture contracts.
- Versioned JSON Schema.
- Ten-agent deterministic fixture.
- Security boundary and handoff.

## Slice 1 - simulator

- Tauri window with transparent, always-on-top behavior.
- Existing-pet placeholder, without copying or modifying the installed asset.
- Detailed layout for one to three agents.
- Compact layout for four to ten agents.
- Grouped layout for more than ten.
- Attention ordering and unread completion behavior.
- Russian interface.
- Fixture playback for lifecycle transitions.
- Native dragging from the titlebar or pet area.
- Explicit close button for the borderless window.

## Slice 2 - local event hub

- Schema validation.
- In-memory store and monotonic sequence handling.
- Minimal local cache.
- Authenticated loopback event endpoint.
- UI snapshot subscription.

## Slice 3 - Codex read-only spike

- Audit supported Codex hook events and trust flow.
- Design an additive configuration entry and rollback, but do not apply it until separately approved.
- Map lifecycle, activity, attention, and completion.
- Confirm that hook failure cannot block normal Codex work.
- Test with root plus multiple subagents.

## Slice 4 - semantic progress

- Add an explicit local reporting command or tool.
- Show truthful `current/total` plans.
- Show readable current actions and completion summaries.
- Fall back to indeterminate activity when no explicit plan exists.

## Slice 5 - pet docking

- Verify independent overlay behavior first.
- Inspect Windows window topology without modifying Codex.
- Dock automatically only if the host pet window can be identified reliably.

## Later

- OpenCode adapter through the same event contract.
- Task and terminal navigation where supported.
- Optional interaction after a separate security review.

## Explicitly out of scope for the MVP

- Editing Codex or OpenCode settings automatically.
- Approving permissions or answering questions.
- Global auto-approve.
- Reading full transcripts for display.
- Cloud synchronization or external telemetry.
- Shipping an installer before fixture and live-session tests pass.

## Exit criteria

- Ten-agent fixture passes without dropped cards.
- Progress semantics tests reject invented or invalid fractions.
- Completed summaries remain until acknowledged.
- The simulator runs without reading or changing provider state.
- Architecture documents and implementation agree.

## Implementation status - 2026-07-17

Completed:

- Slice 0 architecture scaffold.
- React/TypeScript simulator for 1, 3, and 10 agents.
- Detailed and compact layouts.
- Attention ordering, unread completion acknowledgement, and deterministic playback.
- Five model tests, all passing.
- Vite production build.
- Visual QA at the intended 520x760 Tauri window size.
- Tauri 2 configuration and Rust application shell.
- Native Tauri release build and window smoke test.
- User-verified native dragging and close-button behavior.
- Runnable live-first local artifact at `artifacts/PetCrew.exe`.
- Slice 2 authenticated local event hub.
- Dynamic loopback binding, per-install bearer token, CORS allowlist, payload and timeout limits.
- Typed schema validation, monotonic sequence handling, terminal-state protection, and minimal cache.
- Live Tauri snapshot subscription plus a ten-agent hub simulator.
- Eight TypeScript tests and seven Rust core tests, all passing.
- Native hub smoke test covering authentication, replay, oversized payloads, CORS, cache safety, and shutdown cleanup.
- User-verified ten-agent loading and live step changes on 2026-07-18.

Pending:

- Replace the CSS pet placeholder only after the existing pet asset is located safely.
- Audit supported Codex integration points and prepare an exact rollback before any provider configuration change.
- Implement Codex and OpenCode adapters only after their separate approval gates.
