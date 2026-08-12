# Repository boundaries

PetCrew is one source repository with several runtime components. It is not a
backup store for a particular Windows installation.

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

The local `main` history predates this boundary and contains private operational
records in old commits. It must never be pushed to a public remote. Public
publication uses only the disconnected, single-root `codex/public-ready` branch.
Its tree must match the verified source tree, while its author and committer
metadata use the project identity rather than a personal name or email address.

A green working-tree scan does not make another branch or an exported copy of
the whole workspace safe to publish. Run `tools/verify.ps1 -PublicAudit` before
every public push and push the publication branch by its explicit name. Never
use `git push --all` for this repository.

## Release boundary

A public release is produced from source and lockfiles. It does not include an
installed plugin cache, a configured Codex or OpenCode home, scheduled tasks,
runtime secrets, or local rollback archives. Live installation and rollback are
separate explicit operations described by audited activation documentation.
