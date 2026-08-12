# PetCrew Codex plugin contract

Status: implementation contract for the installed local plugin. Repository candidate
`0.1.0+codex.20260718191649` is validated and present in the personal cache; automatic lifecycle
capture still requires a newly started task for live verification.

## Boundary

- Plugin name and folder: `petcrew` / `plugins/petcrew`.
- Marketplace: repository-local `.agents/plugins/marketplace.json`.
- Transport: atomic latest-state registry plus best-effort POST to the running PetCrew local hub at `/v1/events`.
- Discovery: `%LOCALAPPDATA%\app.petcrew.overlay\hub-runtime.json`, with the secret read only from the descriptor's `secret_file`.
- Provider: `codex`; protocol version: `1.0`.
- Hook and MCP failures must never block, cancel, approve, deny, or otherwise change Codex work.
- Repository development does not directly edit live configuration or installed cache. Installation,
  update, hook trust, and rollback remain separate reviewed UI actions.

## Identity and lifecycle

- A visible root card represents one Codex turn. Session, turn, child, and project identifiers are
  stable SHA-256-derived opaque values; raw Codex identifiers are never persisted.
- `SessionStart` is a warm-up/no-op because it has no turn identity and must not create a misleading card.
- Turn-scoped hook events without a non-empty `turn_id` are ignored. The bridge never guesses identity from a title, timestamp, or process id.
- A subagent card is emitted only when the hook supplies a stable `agent_id` or `agentId`. Its parent
  is the opaque turn id.
- Tool events that do not identify a subagent update the root turn only.
- An explicit MCP report uses caller-supplied `session_id`, `agent_id`, and optional `parent_agent_id`; when absent, process-local defaults are used only for that MCP server lifetime.
- Every event gets a UUIDv4 `event_id`. `sequence` uses `time.time_ns()` and is strictly increasing inside one bridge process. Concurrent older arrivals may be rejected as stale by the hub, but cannot overwrite newer state.

## Sanitized hook mapping

| Hook event | PetCrew event | Visible text |
| --- | --- | --- |
| `SessionStart` | none | none |
| `UserPromptSubmit` | `agent.started` | `Начал задачу` |
| `PreToolUse` for `update_plan` | `agent.progress` | completed/total status counts only |
| `PreToolUse` for `request_user_input` | `agent.attention_requested` | `Ждёт выбора` |
| `PostToolUse` for `request_user_input` | `agent.activity` | `Продолжает работу` |
| `PermissionRequest` | `agent.attention_requested` | `Ждёт подтверждения` |
| `SubagentStart` | `agent.started` | `Подключился к задаче` |
| `SubagentStop` | `agent.completed` | `Закончил свою часть` |
| `Stop` | `agent.completed` | `Задача завершена` |

The hook bridge may inspect only event name, session id, turn id, stable agent id, tool name, and
`cwd`. For `update_plan` only, it may count the allow-listed status values in the structured `plan`
array; it ignores step text and all other arguments. It hashes every identifier and derives only the
final directory name from `cwd`; the full path is discarded. It must ignore and never transmit raw prompts, tool arguments, tool results,
transcripts, environment variables, approval decisions, file contents, paths, tokens, or secrets.

## Global latest-state registry

- Location: `%LOCALAPPDATA%\app.petcrew.overlay\agent-registry\`.
- Scope: all hook-enabled Codex projects for the current Windows user.
- Shape: one latest sanitized protocol event per opaque provider/session/agent key.
- Write: bounded JSON to a same-directory temporary file followed by atomic replace.
- Limits: 64 KiB per file and at most 500 imported records.
- Recovery: import at application startup and every two seconds while PetCrew runs.
- Expiry: 24 hours for non-terminal records and seven days for terminal registry files.
- Clear: the bundled `Очистить` action removes both hub cache and registry files.
- The registry is not a queue, transcript, tool log, or history of intermediate events.

## Existing-task bootstrap

- PetCrew must not require a task to be created after plugin installation. At startup and on its
  normal poll, it may read the current user's Codex state index in read-only SQLite mode.
- Discovery is limited to recent non-archived user-visible roots and recent open child-thread
  edges. Internal guardian/reviewer records whose technical `source` is subagent JSON are not user
  task cards; a user-visible delegated task with `thread_source=subagent` but desktop source such as
  `vscode` remains visible. It
  may also read the rollout lifecycle variants `task_started`, `agent_message`, and
  `task_complete` to recover current state and the latest short assistant status. It may additionally
  read only `response_item.type`, `response_item.name`, and `response_item.call_id` for
  `request_user_input` calls and their matching `function_call_output`. It never reads the form
  question, choices, arguments, or answer. Typed parsing ignores `user_message`, reasoning, commands,
  all other tool arguments and output, results, and unknown payload fields.
- A root title comes only from `session_index.jsonl.thread_name`; missing names become `Задача
  Codex`. A child label comes only from `agent_nickname`; missing labels become `Помощник Codex`.
  Raw database titles are not used because they can contain prompt text.
- Full `cwd` is used only in memory to derive an opaque project id and the final directory name; the
  full path is never copied into an event, cache, registry, UI snapshot, or log.
- A started but not completed latest turn is `working`. The typed `task_started` timestamp is
  retained on both working and terminal discovery events. After `task_complete`, both root and child
  cards are terminal with an unread result. If polling misses the short working phase of a newer
  turn, a `started_at` strictly later than the stored terminal `updated_at` proves that this is a
  new result. If a large ignored JSONL record pushes that start outside the bounded tail, a strictly
  newer database sequence on the next terminal observation is the fallback proof. An earlier
  acknowledgement must not carry into either case. Re-reading the same completed turn preserves
  its acknowledgement. A recovered working refresh cannot move an existing turn clock backward.
  An active turn with an unmatched `request_user_input` call is exactly `waiting_input`; the matching
  `function_call_output`, a newer turn, or `task_complete` clears that state. This is a typed
  lifecycle fact, not an inference from elapsed time or message text. `waiting_approval` remains
  available only from `PermissionRequest`; rollout recovery must not guess approval state. The visible
  action/result is the latest sanitized assistant `agent_message`, limited to 160 characters, with
  a generic fallback when absent.
  Messages whose complete normalized body is JSON are treated as internal protocol output and ignored.
- Discovered cards never claim an exact percentage, current tool, approval, or result that the
  lifecycle stream does not provide.
- Automatic hook events remain authoritative. When a matching real root or child event arrives, its
  recovered placeholder is removed rather than shown as a duplicate.
- Index access is fail-soft: missing files, a locked database, or an incompatible schema never
  mutate or block Codex and never trigger an alternate runtime. One missing discovery scan cannot
  remove a visible non-terminal recovered card; rediscovery clears the bounded missing-state grace.

## Explicit semantic reporter

The optional local MCP server exposes one tool: `petcrew_report_status`.

Automatic hooks are the primary live channel and require no model tool calls. A generic PetCrew
mention, ordinary turn, tool call, or multi-step task must not invoke the semantic reporter. The
reporter is opt-in only for an explicitly requested numeric plan, custom readable action/result, or
named child status that hooks cannot represent safely.

Accepted status values: `started`, `working`, `progress`, `waiting_input`, `waiting_approval`, `blocked`, `completed`, `failed`, `cancelled`.

- `task_title` is required and limited to 120 characters.
- `action` and `progress_label` are optional and limited to 160 characters.
- `summary` is optional and limited to 500 characters.
- Step progress is emitted only when both `current` and `total` are integers, `total > 0`, and `0 <= current <= total`.
- A caller may report `4/10`, agent hierarchy, a current action, and a terminal summary explicitly. The bridge never invents numeric progress.
- The tool is observational: a missing or unavailable PetCrew hub returns a non-blocking informational result.

## Failure and runtime policy

- Hook HTTP deadline: at most 250 ms; hook command timeout: 3 seconds on Windows.
- Command hooks are registered only for semantic lifecycle events. `PreToolUse` is limited to
  `request_user_input` and `update_plan`; `PostToolUse` is limited to `request_user_input`.
  Generic tool calls do not spawn a Python hook process.
- Network target is restricted to the loopback endpoint read from the runtime descriptor.
- No retries, telemetry, remote calls, transcripts, or persistent intermediate-event logs. The only
  plugin persistence is the bounded latest-state registry above.
- Hook mode writes nothing to stdout and always exits successfully after bounded validation/posting.
- MCP mode reserves stdout for JSON-RPC and emits no secrets to stderr.
- Implementation uses the machine's standard `python`. A future installation test must stop and report if `python` is the WindowsApps/Store stub or the wrong runtime; it must not install, copy, shim, or select an alternate Python.
- The MCP server is optional (`required: false`). A launch or hub failure cannot prevent Codex startup or task execution.

## Update gate

Before updating an installed copy, validate the plugin and skill, run unit tests, confirm the exact
hook hash, verify the standard Python, and preserve the previous cached version for rollback. Use the
Plugins UI; test updated skills and tools in a new task.
