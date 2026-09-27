# PetCrew Bridge / Relay source candidate

This directory contains the independently installable Bridge/Relay module. It is
an offline promotion candidate; editing it does not change an installed plugin.

Use standard Windows Python (or a project-owned environment). No Python packages
are required. From this directory run:

```powershell
python -B -m unittest discover -s tests
```

`.codex-plugin/plugin.json` points to `.mcp.json`; MCP uses `python`, the relative
`scripts/opencode_bridge.py` entry point and plugin working directory. Skills are
under `skills/`. The module expects the separately configured loopback OpenCode
service and PetCrew Core; tests mock those services and isolate local state.

The machine-specific legacy task installer has been removed from the distributable
source; its historical version remains in Git history and local operational evidence.
This candidate does not provide a portable service installer. Activation requires an
audited exact source-registration/task-action change with backups and rollback.
Do not register a second service, copy secrets into the repository, or edit installed
caches by hand. See the repository installation guide for the component boundaries.

## New-task model selection contract

Job: choose the provider/model for one new OpenCode task without changing the
global OpenCode configuration or existing sessions. The authority is the model
explicitly requested by Lisa; omission retains the accepted Bridge default.

`opencode_start_task` accepts an optional string `model` in `provider/model`
format, for example `opencode/mimo-v2.6-flash-free`. The first slash separates
the provider; a model identifier may itself contain slash-separated segments.
Empty segments, whitespace, and non-string values are rejected before creating
a session or binding. Omit the field to use `opencode/mimo-v2.6-flash-free`.
An explicit value is sent unchanged as OpenCode's `providerID`/`modelID` pair;
Bridge never substitutes another model or provider. Availability, authentication,
quota and pricing are owned by the connected OpenCode service, not inferred from
the identifier. A submitted receipt reports the requested route, not proof of a
successful model response.

Allowed state: a new task's explicit prompt model and its submission receipt.
Forbidden state: changing provider connections, global defaults, prior sessions,
Relay identity or permissions. `opencode_continue` accepts no model override and
preserves the session model; passing `model` there fails before submission.

Fitness acceptance: test default and explicit routes, slash-containing model
IDs, invalid input before mutation, rejected submissions without substitution,
unchanged follow-ups and return binding. Exercise the installed MCP schema and
stdio-to-HTTP request path against an isolated loopback fixture. A real model
response and exact Relay pickup remain a separate live acceptance gate.

## Parent transfer workflow

Inspect the current source parent with `codex_get_return_parent` (read-only, no
workspace required). Transfer to a new source with `codex_transfer_return_parent`
(`transfer_parent: true`, CAS on `expected_binding_id`). A transfer sets
`return_state: parent_only` and does not arm or emit a return. After transfer,
the normal `codex_bind_return` -> send -> `codex_confirm_return` workflow
creates a fresh armed generation from `parent_only` state.

Runtime bindings, receipts, journal and status outbox belong to per-install local
state. Source relocation must preserve identities and delivery state. See
`../../docs/RELAY_RETURN_DISPATCHER.md` and `../../docs/REPOSITORY_BOUNDARIES.md`.
