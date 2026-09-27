# PetCrew

PetCrew is a local Windows companion that shows what a team of Codex and
OpenCode agents is doing, which agents need attention, and which results are
still unread. It keeps prompts, tool payloads, transcripts, credentials, and
environment variables outside the UI and persisted state.

![PetCrew simulator with ten agents](docs/assets/simulator-preview.png)

## What is implemented

- a compact React/Tauri Monitor with list and tile layouts, Russian UI, themes,
  text sizing, honest progress, and bulk acknowledgement of terminal results;
- a separately runnable headless PetCrew Core that owns local observation,
  retained state, authenticated snapshots, and SSE feeds;
- an inert repository-local Codex plugin with sanitized lifecycle hooks and an
  optional semantic status reporter;
- a dependency-free OpenCode adapter with deterministic terminal receipts;
- an offline Bridge/Relay source candidate for delegated OpenCode and Codex
  task submission, durable completion handling, and exact return routing;
- one provider-neutral event protocol and JSON Schema;
- loopback-only authenticated transport with a per-install secret;
- fixture-driven UI, Rust, Python, and Node test suites.

The Monitor and Core observe activity; they do not approve tools, answer
questions or submit prompts. The separately activated Bridge can submit explicitly
delegated work through provider interfaces. Neither component bypasses provider
security boundaries.

## Repository map

```text
apps/overlay/       React Monitor and Tauri/Rust Core
adapters/opencode/  dependency-free OpenCode adapter
plugins/petcrew/    distributable Codex plugin source
plugins/opencode-bridge/  independently installed Bridge/Relay source candidate
shared/schemas/     provider-neutral event contract
tests/fixtures/     deterministic cross-component fixtures
docs/               product, architecture, protocol, and security truth
tools/              repository verification helpers
```

See [repository boundaries](docs/REPOSITORY_BOUNDARIES.md) for the exact
public-source and local-operations split.

## Development

Prerequisites:

- Windows with Microsoft C++ Build Tools (Desktop development with C++) and
  the Windows SDK; WebView2 Runtime is required to run the Monitor;
- Node.js 24 and npm compatible with `apps/overlay/package-lock.json`;
- stable Rust with Cargo and the MSVC host toolchain;
- one normal Python 3 installation available as `python` (CI uses 3.12).

See [Tauri's Windows prerequisites](https://v2.tauri.app/start/prerequisites/#windows)
and [local build acceptance](docs/INSTALLATION.md#build-reproducibility).
Install project JavaScript dependencies once with `npm ci` in `apps/overlay`
before running the verifier.

Run every source-level check from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\verify.ps1
```

Run the Monitor in development mode:

```powershell
Set-Location .\apps\overlay
npm ci
npm run tauri:dev
```

Build the canonical Windows Monitor candidate:

```powershell
Set-Location .\apps\overlay
$env:CARGO_BUILD_JOBS = '2'
(Get-Process -Id $PID).PriorityClass = 'BelowNormal'
npm run tauri:build
```

Do not use a direct `cargo build --release --bin petcrew` as the packaged
Monitor: it does not embed Tauri's production frontend protocol. The headless
`petcrew-core` binary remains a normal Rust binary.

Building source does not install provider adapters, change Codex/OpenCode
configuration, register scheduled tasks, or start background services. See
[installation](docs/INSTALLATION.md) before any live activation.

## Product rules

1. Show every real active or attention-requiring agent.
2. Use `current/total` only when both values come from an explicit plan or
   semantic status report.
3. Keep completed results visible until acknowledgement or an explicit
   retention rule.
4. Prefer short human-readable actions over raw implementation noise.
5. Keep the provider boundary replaceable and the transport local-only.

## Documentation

- [Документация для пользователя](docs/README.md)
- [Что такое PetCrew](docs/USER_GUIDE.md)
- [Как появляются новые котики](docs/ADDING_AGENTS.md)
- [Настройки](docs/SETTINGS.md)
- [Product specification](docs/PRODUCT_SPEC.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Event protocol](docs/EVENT_PROTOCOL.md)
- [Security boundaries](docs/SECURITY.md)
- [Licensing](docs/LICENSING.md)
- [Installation and rollback](docs/INSTALLATION.md)
- [Codex plugin contract](docs/CODEX_PLUGIN_CONTRACT.md)
- [OpenCode plugin contract](docs/OPENCODE_PLUGIN_CONTRACT.md)
- [Adding a provider](docs/NEW_PROVIDER_INTEGRATION.md)
- [Contributing](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)

## Status

The recovered source baseline has UI, Rust, Codex plugin, OpenCode adapter, and
Bridge/Relay test suites, plus TypeScript and Vite production builds. The
Bridge/Relay directory is a source candidate; repository changes do not update
its separately installed cache or services. One native Codex canary proved a
foreground send and exact reply, but did not prove autonomous background Relay
delivery. Live installation acceptance is tracked separately from source
correctness.

## License

PetCrew source is licensed under MIT. Third-party dependencies and tools keep
their own licenses. See [LICENSE](LICENSE), [Licensing](docs/LICENSING.md), and
[Third-party notices](THIRD_PARTY_NOTICES.md).

See [baseline status](BASELINE_STATUS.md) for the recovered source scope and release limits.
