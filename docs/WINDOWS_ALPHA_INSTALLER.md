# Windows alpha installer contract

## Purpose

The first PetCrew installer is a current-user Windows alpha package for one
bounded external acceptance test. It proves that a clean workstation can
install, start, restart, and remove the PetCrew runtime without using the
source checkout.

It is not yet a generally supported release, an automatic provider setup
wizard, or an auto-updating application.

## Package contents

The NSIS installer contains:

- `PetCrew.exe`, the on-demand Monitor;
- `petcrew-core.exe`, the headless local Core;
- the PetCrew MIT license and the release third-party notices;
- Start menu and uninstaller metadata owned by the PetCrew package.

Installation is per-user and does not require administrator rights. PetCrew
Core is registered as the exact current-user scheduled task `PetCrew Core` and
is started after installation. The Monitor is never added to Windows startup.

## Explicit exclusions

The alpha installer does not:

- edit Codex or OpenCode configuration;
- install or trust provider plugins automatically;
- replace an existing `notify` command or another provider hook;
- install Python, Node.js, Rust, Git, or a second provider runtime;
- enable telemetry, cloud transport, or remote listening;
- delete PetCrew user data during ordinary uninstall.

Provider activation remains a separate, reversible, explicitly approved step
through each provider's supported installation surface.

## Security and runtime behavior

- Core binds only to loopback and creates its per-install bearer secret on
  first start in the application data directory.
- The scheduled task runs only for the installing interactive user with least
  privilege, ignores duplicate starts, has no execution time limit, and uses a
  bounded restart-on-failure policy.
- Upgrade replaces package-owned binaries but preserves the application data
  directory and its existing secret.
- Uninstall first stops and removes only the exact `PetCrew Core` task, then
  removes package-owned files. Provider adapters and user data are left
  untouched because they have separate ownership and rollback contracts.

## Build contract

Run from the repository root:

```powershell
.\tools\build-windows-alpha.ps1
```

The build must:

1. require a clean tracked working tree;
2. verify that Cargo, Tauri, and npm package versions agree;
3. run the canonical repository verifier;
4. build the standalone Core for the Windows MSVC target;
5. verify that Tauri includes the standalone Cargo binary exactly once;
6. merge the installer-only `tauri.alpha.conf.json` over the ordinary
   development configuration and build one current-user NSIS installer;
7. stage the installer, checksums, manifest, and license files under an ignored
   versioned release directory;
8. fail if any expected artifact is absent or ambiguous.

The first NSIS build may download Tauri's pinned NSIS 3.11 tool archive and
its companion plugin into the Rust target cache. This is a build dependency,
not a system-wide installation.

## Acceptance and rollback

The installer is accepted only after a separately approved clean-Windows test:

1. verify the published SHA-256 before running it;
2. install as the ordinary test user;
3. confirm exactly one `PetCrew Core` task and one healthy Core owner;
4. open and close the Monitor without stopping Core;
5. activate one provider adapter through its supported route and confirm one
   task lifecycle exactly once;
6. perform an ordinary Windows sign-out/sign-in or restart and confirm Core
   recovers without opening Monitor;
7. uninstall PetCrew and confirm the exact Core task and package files are
   gone while unrelated provider configuration is unchanged.

Rollback is the package uninstaller. If provider activation was also tested,
roll it back separately through that provider's documented route. User data
removal is a separate explicit privacy action.

## Known alpha limitation

The first package is unsigned. Windows SmartScreen may therefore warn before
execution. A trusted code-signing certificate is required before presenting
the installer as a low-friction general release.
