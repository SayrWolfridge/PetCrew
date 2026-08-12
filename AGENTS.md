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
