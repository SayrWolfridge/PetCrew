# PetCrew agent instructions

## Scope

Work only inside the repository root unless the user explicitly expands scope.

## Before implementation

1. Read `PROJECT.md`.
2. Read `_Agents/HANDOFF.md` and `_Agents/SHARED/DECISIONS.md`.
3. Check `git status --short` when the project becomes a Git repository.
4. Update the relevant contract document before changing a cross-component interface.

## Safety

- Do not modify `~/.codex`, OpenCode configuration, installed plugins, the Codex application, or pet assets during simulator work.
- Before a future live-adapter step, create an explicit backup and rollback plan and show the exact intended configuration change.
- Do not implement approvals or answers during the read-only MVP.
- Bind local services to loopback only and require a per-install secret before accepting live events.
- Do not persist raw prompts, transcripts, command bodies, credentials, or environment variables by default.

## OpenCode process recovery authority

- Lisa delegates Codex standing authority to terminate an exact, proven-hung OpenCode process when
  it blocks the standard PetCrew Bridge route; no fresh per-incident approval is required after the
  validation below succeeds.
- Before termination, resolve the exact PID and command line and prove by read-only checks that the
  process belongs to the known OpenCode CLI/Bridge route, is orphaned or fails bounded official
  health/API checks, blocks the standard route, and has no evidenced active prompt, file write,
  build, or healthy GUI session. If activity or ownership is uncertain, stop and ask Lisa.
- Stop only that validated PID or validated process tree. Never kill by process name, wildcard,
  broad port sweep, or guessed replacement PID; never stop unrelated Node, Codex, PetCrew, GUI,
  build, or user processes under this authority.
- After termination, restart only the existing standard scheduled Bridge task when required and
  verify its task state, loopback listeners, health, and Relay path. Do not create a parallel
  server, alternate runtime, shim, or configuration change. Report the PID, evidence, action, and
  result. The operational recovery is restart of the existing task; no file state is rolled back.

## Host-safe native builds

- On Lisa's interactive Windows workstation, every native or Tauri release build must run at
  `BelowNormal` process priority with `CARGO_BUILD_JOBS=2`. Set both limits before invoking the
  canonical build command so Cargo and all inherited `rustc` processes remain constrained.
- Run only one heavy native build at a time. Do not overlap it with another Tauri/Rust build,
  dependency installation, broad test suite, or other known high-load project operation.
- Do not run `cargo clean` or discard a valid target cache unless a demonstrated build defect
  requires it. An isolated clean candidate is allowed, but its first native compile must use the
  same priority and job limits.
- Keep `npm run tauri:build` as the canonical Monitor route. Resource limits do not authorize a
  direct-Cargo substitute or another build tool.
- During a long build, report progress at useful intervals and verify that `cargo`/`rustc` inherit
  `BelowNormal`. If the workstation becomes unresponsive, stop only the exact build tree; do not
  kill unrelated Node, Codex, OpenCode, or PetCrew processes.
- Iterate presentation-only changes through focused UI tests, `npm run build`, and browser QA.
  Do not rebuild or reinstall the native executable after every visual tweak. Produce one native
  candidate when the accepted change batch is ready for live verification, or earlier only when a
  Rust/native boundary itself changed and needs compilation evidence.
- Never deploy a release built from a working tree that contains unrelated or unaccepted changes.
  When the tree is mixed, create an isolated candidate from the verified production base and add
  only the reviewed release delta plus required already-deployed compatibility fixes. Verify the
  candidate's included scope before building.

## Progress semantics

- `current/total` is allowed only when both values come from an explicit agent plan or status report.
- Inferred activity may describe the current tool or phase but cannot invent a percentage or denominator.
- A completed result remains visible until acknowledged or expired by an explicit retention rule.

## User-facing language

- Russian is the default interface language.
- Show short meaningful actions such as `Проверяет события Codex`, not raw implementation noise.
- Surface all real agents; use density changes and grouping instead of silently dropping cards.

## Coordination

- Append dated entries to `_Agents/LOG.md`.
- Keep the current handoff short and accurate.
- Put durable product and architecture decisions in `_Agents/SHARED/DECISIONS.md`.
- Keep Codex and OpenCode private notes in their own subfolders.
