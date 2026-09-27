# Local working baseline

Source candidate based on public commit 475725fd51a6053c7a114bd21adf34770028a04e with
retained Monitor/Core production delta and OpenCode late-terminal repair.

Includes handle-based Core ownership, reset preserving completion/replay state, summary filters,
existing Relay recovery control and late assistant completion after early OpenCode idle.

Excludes external-file wake and installer.
Current work is local repository maintenance and reliable Monitor/Relay operation. Installer work
is parked; binary publication is outside the active scope.
No live installation was replaced by this reconstruction.

Verified: 48 UI tests, 64 Rust tests, 12 Codex connector tests, 25 OpenCode adapter tests and the
frontend build. Core/UI checks used byte-identical retained source with existing dependencies/cache;
this was not a clean-machine dependency install or a new native release build.

## Integrated offline candidate — 2026-09-06

Updated_at: 2026-09-06T03:40+03:00.

Now includes the accepted guardian cleanup and cold-cache navigation repair, the
narrow Core Relay reactivation change, and Bridge/Relay source under
plugins/opencode-bridge/. The original baseline evidence above remains historical.
Current validation: 160 Python Bridge tests, 78 combined Rust tests, 48 UI tests,
web build, and the unchanged two-receipt regression against imported modules.
Canonical native build and isolated smoke evidence are recorded in the managing
task's local integration receipt. This is a source candidate, not live acceptance
of Relay. Existing installation and source registration remain unchanged.
