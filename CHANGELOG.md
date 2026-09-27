# Changelog

All notable changes to PetCrew are recorded here. The project follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); release versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Project-grouped list mode with independently persisted disclosure state for projects and cards;
  tile mode remains the compact live overview.
- Guarded OpenCode project navigation for already registered projects, including a bounded Windows
  UI Automation compatibility route for OpenCode Desktop 1.18.32.
- Source candidates for the explicit OpenCode/Codex Bridge and return Relay, with durable exact
  completion receipts and parent-only Codex transfer.
- Snapshot-first Codex Desktop diagnostics and explicit recovery controls that operate only on an
  exact verified process tree.
- Optional on-demand Penpot plugin source isolated from the normal PetCrew and Codex runtimes.

### Changed

- Settings use compact semantic rows; long inspection output wraps, and the live snapshot owns the
  persistent scrollbar used by both list and tile modes.
- Monitor reset clears presentation state while retaining registry, journal, Relay, and recovery
  evidence needed to restore truthful state.
- CI now audits full public history and requires immutable commit SHAs for external GitHub Actions.
- Vitest is pinned to 4.1.11.

### Security

- Relay health requires the exact loopback service contract instead of accepting any HTTP response.
- Forced Relay recovery records and revalidates PID, executable, command line, and creation time
  before terminating a process.
- Dependency and workflow-action advisories found during release hardening are resolved.

### Fixed

- Preserved late OpenCode terminal results, Relay returns, acknowledged results, and partial Codex
  rollout state across cache refreshes and writer conflicts.
- Restored mouse-wheel scrolling in reduced native windows without taking over native resize.
- Filtered internal guardian tasks without dropping user-visible delegated work.
