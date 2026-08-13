# Installation and rollback

PetCrew source, provider activation, and Windows autostart are separate scopes.
Cloning or building the repository performs no installation.

## Build the Monitor and Core

From `apps/overlay`:

```powershell
npm ci
npm test -- --run
npm run build
npm run tauri:build
```

`npm run tauri:build` is the canonical Monitor build route. A direct Cargo
release build of the desktop binary does not package Tauri's production web
assets correctly. `cargo build --release --bin petcrew-core` may be used for the
headless Core after the Rust test suite passes.

For the bounded Windows alpha installer, use the repository-level release
wrapper instead:

```powershell
.\tools\build-windows-alpha.ps1
```

It builds the Monitor and standalone Core together, invokes the pinned Tauri
NSIS bundler, and writes the installer plus its manifest and checksums only to
the ignored release-artifact directory. See
[Windows alpha installer contract](WINDOWS_ALPHA_INSTALLER.md).

Build output is local. Do not commit executables, runtime descriptors, lock
files, secrets, caches, or rollback archives.

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
They are not created by an ordinary repository build. The Windows alpha
installer is the single documented exception: it registers only the exact
current-user `PetCrew Core` task through the installed Core executable and its
uninstaller removes that same task. Manual and developer installations still
require their own exact backup, configuration diff, health check, and rollback
plan. Executable and working-directory fields must both point to the external
installation directory.

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
