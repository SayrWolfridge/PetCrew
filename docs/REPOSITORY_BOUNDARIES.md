# Repository boundaries

PetCrew is one source repository with several runtime components. It is not a
backup store for a particular Windows installation.

## Module ownership (accepted 2026-09-05)

Updated_at: 2026-09-05T22:32+03:00.

PetCrew has one source repository and independently installed modules:

- Core owns event ingestion, retained agent state, and the completion journal.
- Monitor owns presentation and user actions.
- Provider adapters normalize provider events into the shared protocol.
- Bridge/Relay owns task submission, exact result routing, and the return-processing queue.

The canonical Bridge/Relay development source is `plugins/opencode-bridge/`,
alongside `plugins/petcrew/`. Source consolidation is complete; runtime activation
is separate and remains pending. Bridge began as a personal cross-project plugin,
which explains the external source still used by the current installation.

Both modules are listed in the repository marketplace as independently available
plugins. Listing does not install either module. Keep one development source;
the old installation is a deployment boundary, not a second development tree.
Any installed source-registration change requires the exact backup and rollback
plan, including preservation of existing plugin identity and runtime state.

The canonical public integration branch is `main`. Release-hardening or feature work may use an
isolated `codex/*` branch and managed worktree, but only a verified candidate is merged into
`main`. A worktree path is an execution detail, not a second source of truth. Use Git worktree
operations for isolated checkouts and verify their revision and clean state before integration.

Editing repository source must not change the running installation. Monitor,
Core, and Relay retain separate lifecycle boundaries; closing Monitor must not
implicitly stop autonomous Relay processing. Runtime bindings, secrets, journals,
installed caches, binaries, and machine configuration stay outside versioned source.
No source reorganization authorizes binary publication or a live reinstall.

## Public source

The following paths form the publishable product:

- `apps/overlay/` — PetCrew Monitor, headless Core, and their shared Rust state engine;
- `adapters/` — provider adapters, currently OpenCode;
- `plugins/` — distributable Codex plugin source;
- `shared/` — provider-neutral schemas and contracts;
- `tests/` — cross-component fixtures;
- `docs/` — product, architecture, protocol, security, and contributor documentation;
- `tools/` — deterministic repository verification helpers.

Dependencies are owned by the component that uses them. JavaScript dependencies
and their lockfile live under `apps/overlay`; Rust dependencies and `Cargo.lock`
live under `apps/overlay/src-tauri`. The OpenCode adapter is intentionally
dependency-free. The Codex bridge uses the standard project Python runtime and
does not own a separate environment.

## Local-only state

The following data belongs to one machine or one development session and must
never be published:

- installed executables and timestamped rollback copies in a separate
  machine-local operations directory outside the checkout;
- runtime descriptors, lock files, tokens, caches, Relay bindings, task/session
  identifiers, and completion-delivery journals;
- temporary smoke-test directories and diagnostic scripts in that external
  operations directory;
- local installation paths, scheduled-task exports, and live configuration copies;
- raw agent work logs and generated local project-control cards.

These files may remain on disk for rollback or diagnosis, but not inside the
repository tree. `.gitignore` retains guards for legacy `artifacts/backups/` and
`tmp/` paths so an old script cannot stage them accidentally; it is not a
backup, separation, or retention policy.

## Internal coordination

`_Agents/` is the local engineering handoff area. Durable product and
architecture decisions may be curated into public documentation. Raw logs,
machine-specific evidence, process identifiers, task identifiers, and rollout
receipts are not part of a public release.

The canonical `main` branch starts from one audited public root and grows like a
normal source history. Feature branches must descend from that same root. Every
commit uses the project identity rather than a personal name or email address.
The retired private development history is stored only as an external local
bundle and is not a branch, tag, or object reachable from this repository.

A green working-tree scan does not make arbitrary history or an exported copy
of the whole workspace safe to publish. Run `tools/verify.ps1 -PublicAudit`
before the first public push and in CI. The audit checks every local branch and
tag for a shared public root, project-only authorship, forbidden operational
paths, and sensitive-looking content.

Normal development uses `main`, short-lived feature branches, commits, pushes,
and pull requests. Runtime installation, rollback evidence, local coordination,
and the retired private-history bundle remain outside public Git history.

## Release boundary

A public release is produced from source and lockfiles. It does not include an
installed plugin cache, a configured Codex or OpenCode home, scheduled tasks,
runtime secrets, or local rollback archives. Live installation and rollback are
separate explicit operations described by audited activation documentation.
