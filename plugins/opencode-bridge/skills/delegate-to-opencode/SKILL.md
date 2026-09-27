---
name: delegate-to-opencode
description: Safely delegate a new task or follow-up to OpenCode through OpenCode Bridge, retain its exact terminal result in PetCrew, and pick it up in the originating Codex task after the user nudges that task when needed. Use when the user asks Codex to send, hand off, continue, assign, audit, implement, or ask something in OpenCode, including Russian requests such as передай OpenCode, отправь задачу в OpenCode, продолжи сессию OpenCode, отдай OpenCode на аудит, спроси у OpenCode, or верни результат сюда.
---

# Delegate to OpenCode

Use the bridge as an asynchronous persistent parent-child handoff. Never poll OpenCode.

## Capability-aware instruction contract

The calling Codex task owns diagnosis, architecture, semantic choices and final
acceptance. Treat OpenCode as an executor whose current capability may be lower
than the coordinator's. Do not send a broad objective and expect the worker to
discover the safe route by itself.

Before dispatch, resolve every question that can be settled from current source,
project rules and read-only evidence. Then give OpenCode a bounded execution
packet containing:

1. the exact objective and allowed workspace;
2. the exact files, functions, stable keys or rows to inspect or edit;
3. the observed defect and its named evidence or canonical source;
4. the required minimal change, including invariants that must stay unchanged;
5. one immediate action followed by its exact check and expected result;
6. the next action only after that check is green;
7. explicit stop conditions, prohibited scope and the evidence to return.

For a multi-step repair, use short sequential gates rather than one large prompt.
Make each gate executable and locally verifiable. If the worker stalls after
describing the next action, repeats a plan, or reports only partial progress, do
not merely say "continue". After the user explicitly asks for a status check or
follow-up, obtain a fresh session read, identify the exact unfinished gate, and
send one concrete same-scope instruction with the command/check and expected
outcome. Never poll, spam reminders, expand scope, or delegate a fresh semantic
decision to compensate for an under-specified packet.

Model names are operational facts, not permanent capability tiers. Apply this
contract from the worker's demonstrated behavior and the risk of the task. When
the necessary diagnosis or design remains ambiguous, keep it in Codex or route
it to an explicitly authorized stronger reviewer before asking OpenCode to edit.

## Workflow

1. Confirm the exact workspace and whether to create a new OpenCode session or continue an existing one.
2. Read the current Codex task id from its own environment:

   ```powershell
   $env:CODEX_THREAD_ID
   ```

   Require one UUID-shaped value. If it is missing or invalid, stop before sending and report that the exact return route is unavailable.
3. Use `opencode_start_task` or `opencode_continue` with:
   - the exact workspace;
   - the bounded prompt;
   - the exact `codex_thread_id`;
   - `wake_on_completion: true`;
   - `agent: plan` unless implementation was authorized.
   New sessions started through `opencode_start_task` default to
   `opencode/mimo-v2.6-flash-free`. If Lisa selected another route for this new
   task, pass the exact optional `model` string in `provider/model` format.
   Do not infer a provider/model change from a general task or token-economy
   request. Omit `model` when no override was selected; never substitute a route
   after an error. Follow-ups sent with `opencode_continue` accept no model
   override and preserve the existing session model. Bridge does not inspect provider quotas and must not
   claim that it will notice a free limit ending or automatically switch models.
4. Check the tool result. It must say `state: submitted`, `wake_on_completion: true`,
   and `persistent_parent_binding: true`.
5. End the Codex turn after reporting the submitted OpenCode session id and the model state in
   plain language so the user can decide whether to change it in OpenCode Desktop:
   - for a new session, name the exact `model` returned in the submission
     receipt; this confirms the requested route, while the actual assistant
     receipt confirms execution. Do not describe an explicit override as free
     merely because the Bridge default is free;
   - for a follow-up, say that Bridge preserves the session model and name the last confirmed
     model only when it is known from the session receipt or the user's explicit report;
   - when the current model is not known, say so. Never present an inherited or assumed model as
     confirmed.

   If the user reports changing the model in OpenCode Desktop, acknowledge that choice and use it
   as the last user-confirmed model for later follow-ups. Do not start a model call, poll, or read
   an unqualified latest response merely to discover the model.

   On Codex Desktop for Windows, do not promise that Relay will automatically continue the open
   source task. Say instead that PetCrew will save the exact result and show a Monitor card; after
   the card appears, Lisa should nudge the source task with a short message such as
   `забери ответ OpenCode`. Codex then reads the saved receipt and continues. This is the expected
   current workflow, not an exceptional failure. Do not say `я сама узнаю`, `ответ вернётся сюда
   автоматически`, or an equivalent certainty while Desktop may retain the task writer.
6. After either a genuine Relay resume or Lisa's pickup nudge, obtain the exact stored
   `completion_id` and terminal `phase` for the bound session from the Relay/Monitor receipt path.
   Call `opencode_read_session` once with that receipt and the exact session, continue the requested
   Codex-side review from the returned matching assistant message, and do not resend the OpenCode
   task or substitute the unqualified latest response. For `failed` or `cancelled`, read the exact
   session without a receipt filter and report that terminal state.

The binding belongs to the OpenCode session, not to one submitted prompt. Later terminal events
from the same session are retained and routed to the same parent even when the follow-up was sent
from OpenCode Desktop or its CLI. They may resume an independently writable task, but an open
Windows Desktop task can require Lisa's pickup nudge. Do not re-arm the binding after every manual
follow-up.

## Standing repair authorization (Lisa, 2026-09-05)

An audited, concrete repair or missing-test follow-up inside an already approved
implementation objective/workspace may be sent to the same bound session with
`agent: build` without asking Lisa again, including from an exact Relay return.
Write a small versioned prompt and handoff with evidence, allowed writes, checks
and stop condition first. Do not resend the original task: send only the new
repair delta once, record dispatch and end the turn under the normal protocol.
Reject duplicate/stale events. After two failed repairs of the same defect,
pause the maker loop and document a changed diagnostic approach.

This is not authority for new behavior/features, expanded scope, dependency
installation/upgrades, real recordings outside agreed tests, external data
transfer, other-project changes, new task creation or binding transfer. A next
stage needs explicit execution approval in the agreed plan. Existing permission
and safety gates still apply; do not permanently grant worker permissions.

## Ownership

- A different Codex task must not take over an already-bound OpenCode session silently.
- Set `transfer_parent: true` only after the user explicitly approves moving that session
  to the current Codex task.
- Use `opencode_detach` only when the user asks to disconnect the session or end the
  parent-child relationship. Detaching stops later completions from waking Codex.

## Failure boundaries

- Do not call `opencode_wait` as a polling substitute after arming Relay.
- If the Relay prompt lacks a valid `completion_id`, or exact receipt lookup fails, report
  a bridge-contract failure instead of reading an arbitrary latest assistant response.
- Treat missing `codex_thread_id`, a rejected binding, `wake_on_completion: false`, or
  `persistent_parent_binding: false` as a bridge failure. Do not silently leave the user
  expecting automatic in-chat continuation. A successful binding means exact durable capture and
  routing; it does not override Codex Desktop's single-writer lock.
- If Bridge reports an empty workspace `.git` marker, the OpenCode task was not submitted.
  Do not retry it. Preserve the marker and ask for explicit approval of a workspace repair;
  Bridge never deletes or initializes Git metadata automatically.
- Do not use `agent: build` without authorization to change the target workspace.
- If OpenCode asks a question or permission, surface the exact bounded request to the user. Never grant a permission permanently.
