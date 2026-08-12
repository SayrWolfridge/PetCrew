# Contributing

PetCrew is a privacy-minimal local observer. Changes must preserve the boundary
between provider activity metadata and private task content.

## Before changing code

1. Read `docs/PRODUCT_SPEC.md`, `docs/ARCHITECTURE.md`,
   `docs/EVENT_PROTOCOL.md`, and `docs/SECURITY.md` for the affected surface.
2. Update the relevant contract before changing a cross-component interface.
3. Keep provider adapters independently testable and keep shared event meaning
   in `shared/schemas/agent-status.schema.json`.
4. Do not add prompts, transcripts, tool bodies, credentials, environment
   variables, or full workspace paths to events, caches, fixtures, or logs.

## Verification

Run:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\verify.ps1
```

The command covers the React build and tests, Rust formatting and tests, Codex
bridge tests, OpenCode adapter tests, JSON syntax, Git whitespace, and public
working-tree hygiene.

## Pull requests

Keep changes scoped by component. Explain any protocol or persistence change,
its privacy impact, compatibility behavior, and rollback. Do not attach local
runtime files, screenshots containing task content, installed caches, binding
journals, or rollback archives.
