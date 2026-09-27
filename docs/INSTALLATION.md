# Installation and rollback

PetCrew source, provider activation, and Windows autostart are separate scopes.
Cloning or building the repository performs no installation.

## Build the Monitor and Core

From `apps/overlay`:

```powershell
npm ci
npm test -- --run
npm run build
$env:CARGO_BUILD_JOBS = '2'
(Get-Process -Id $PID).PriorityClass = 'BelowNormal'
npm run tauri:build
```

`npm run tauri:build` is the canonical Monitor build route. A direct Cargo
release build of the desktop binary does not package Tauri's production web
assets correctly. `cargo build --release --bin petcrew-core` may be used for the
headless Core after the Rust test suite passes.

Build output is local. Do not commit executables, runtime descriptors, runtime
lock files, secrets, caches, or rollback archives. Keep dependency lockfiles
(`package-lock.json` and `Cargo.lock`) committed with the source.

Run one native build at a time on an interactive workstation. Keep the process
priority and Cargo job limit above for inherited compiler processes. Preserve
valid build caches during ordinary iteration.

## Build reproducibility

Use an exact committed source revision with both lockfiles intact. A fresh
checkout or Git archive must contain no copied `node_modules`, `dist` or Rust
`target` directory, personal plugin configuration, credentials or runtime state.
Record the source revision and the installed Node/npm, Rust/MSVC and Python
versions before running the commands above. Use the same canonical Tauri route;
an archive does not need Git for the native build itself.

For a deliberate clean-target check, give that isolated candidate its own empty
Cargo target directory. Do not delete a working checkout's target cache. Record
whether package registries, toolchains and user profiles were shared with the
development machine. A successful build in a second directory proves that the
source does not depend on the original checkout or copied compiled artifacts;
it does not prove a second computer or a clean user profile works.

Second-computer acceptance requires the documented prerequisites on that
computer, dependency restoration from the lockfiles, a successful native build,
and recorded output hashes. Launching the resulting application and activating
connectors are separate runtime acceptance steps. Do not copy live secrets or
installations merely to make the build pass.

## Codex plugin

The source bundle is `plugins/petcrew/`; the repository marketplace descriptor
is `.agents/plugins/marketplace.json`.

Before activation:

1. Run `python -m unittest discover -s plugins/petcrew/tests`.
2. Review the plugin manifest, MCP definition, hook commands, and current hook
   hashes.
3. Back up the existing Codex configuration and any personal marketplace file.
4. Confirm that the proposed change affects only PetCrew-owned entries.
5. Choose an explicit restart or new-task window if the current Codex version
   requires it.

Install through the supported Codex plugin surface for the current version.
Do not hand-edit an installed cache, replace an existing `notify`, add a second
Python runtime, or bypass hook trust. Review and trust only the exact current
PetCrew hook commands.

The MCP reporter is optional (`required: false`). A closed or unavailable
PetCrew must never block ordinary Codex work.

## OpenCode adapter

The source adapter is `adapters/opencode/petcrew.js`. It is inert inside the
repository. Before copying it to a supported OpenCode plugin directory:

1. Run `node --test adapters/opencode/petcrew.test.mjs`.
2. Review the current adapter audit and privacy contract.
3. Back up only the existing target file if one exists.
4. Show the exact destination and restart impact.
5. Activate only after explicit approval.

Do not create a second OpenCode server, wrapper, portable runtime, or alternate
configuration to conceal a failed standard installation route.

## Bridge and Relay

The repository source candidate is `plugins/opencode-bridge/`. It is distinct
from every installed plugin cache and from the machine-local services and state
that run it. Editing or testing this directory does not deploy it.

The repository's `.agents/plugins/marketplace.json` lists both `petcrew` and
`opencode-bridge` as independently available local plugins. Register the source
repository through the normal Codex plugin installation flow when activation
is approved; the catalog file itself neither installs a plugin nor starts a service.
For an existing installation, review the current marketplace/plugin identity
before changing its source so the update does not create a duplicate installation.

The module boundaries are:

- the source directory contains the MCP entry point, Relay code, skills, and
  tests;
- an installed plugin cache is the copy discovered by Codex and must be changed
  only through an audited registration or update operation;
- the external OpenCode server accepts Bridge requests on an authenticated
  loopback endpoint;
- the Codex reader exposes the supported local read path used to correlate an
  exact target task; it is separate from the writer used for foreground sends;
- the Relay queue owns durable pending/completion delivery state and must keep
  working independently of the Monitor window;
- PetCrew Core owns the authenticated event feed and visible retained status,
  not task submission or provider result routing.

Before any activation or source-registration change:

1. Run `python -B -m unittest discover -s plugins/opencode-bridge/tests` from
   the repository root.
2. Review `.codex-plugin/plugin.json`, `.mcp.json`, the skill files, relative
   resource paths, loopback endpoints, and the exact installed destination.
3. Record hashes of the source candidate and current installed cache, and back
   up the current registration, task action, and Relay state needed for rollback.
4. Show the exact registration/task diff, restart impact, health checks, and
   rollback commands before applying them.
5. After activation, verify the installed hashes, task state, loopback listeners,
   authenticated health, queue recovery, and one exact end-to-end return.

The candidate in this repository is not deployed by the build or canonical
verifier. A successful native foreground Codex canary proves only that the
supported send path can create a turn and return its answer. It does not prove
that the background reader and Relay queue will discover, claim, deliver, and
recover that result automatically.

No portable Bridge service installer is provided by this candidate. Do not
edit an installed cache in place, copy secrets into source, or create a second
server or Relay as a fallback. Registration and service changes require their
own explicit installation gate and rollback evidence.

## Runtime services

PetCrew services must bind to loopback only. Core owns the runtime descriptor,
secret, state cache, registry import, and completion feed. Monitor is an
on-demand viewer and must not become an implicit second Core owner.

An installed Core must run from a per-user application directory outside the
source checkout. The repository `artifacts/` directory is build output, not an
installation location. Rollback binaries, scheduled-task exports, runtime
snapshots, and diagnostic scratch data also belong in a separate machine-local
operations directory outside the repository.

Scheduled tasks or other autostart mechanisms are machine-local operations.
They are not created by the repository build and must have their own exact
backup, configuration diff, health check, and rollback plan. Their executable
and working-directory fields must both point to the external installation
directory.

## Acceptance

After a separately approved activation:

1. With PetCrew closed, provider work completes normally.
2. With Core available, one new task appears, updates, and reaches a terminal
   state exactly once.
3. Waiting-for-input and waiting-for-approval appear only from explicit
   structural provider signals.
4. No prompt, response body, tool payload, credential, environment variable, or
   full workspace path enters PetCrew persisted state.
5. Existing provider configuration outside PetCrew-owned entries is unchanged.

## Rollback

1. Disable or uninstall PetCrew through the same supported provider surface.
2. Verify a new provider task produces no PetCrew event.
3. Compare current configuration with the pre-activation backup.
4. Remove only PetCrew-owned entries and files.
5. Do not restore an entire configuration file without reviewing unrelated
   changes made after the backup.
6. Restart provider applications only in an explicitly approved window.

Removing an installed adapter does not delete this repository, local build
artifacts, or separately retained rollback evidence.
