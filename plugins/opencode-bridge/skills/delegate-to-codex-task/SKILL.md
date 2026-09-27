---
name: delegate-to-codex-task
description: Send an authorized one-way notice to an existing Codex task, or delegate work with an exact event-driven return through PetCrew Relay when the result must come back. Use for requests such as просто скажи, уведоми, передай что, напиши в соседнюю сессию, отправь Луне, продолжи другую задачу, верни результат сюда, or пусть задача сама вернётся. Choose the return mode from the user's intended outcome; a notice does not require a return binding.
---

# Delegate to Codex Task

Use the existing Codex task tools for messages. Use PetCrew Relay only when the user needs a
durable event-driven return. Never replace return delivery with a heartbeat or repeated waits.

## Select the delivery mode (Lisa, 2026-09-26)

- A one-way notice shares a fact, reminder, or accepted artifact without requiring a result here.
  Requests such as `просто скажи`, `уведоми`, or `передай, что сделали релиз и его надо не потерять`
  use this mode. Decide from the intended outcome, not an isolated verb such as `отправь`.
- Delegated work whose result must return to the originating task uses the automatic-return
  workflow below. Preserve its binding and receipt guarantees.
- Do not silently downgrade a requested automatic return to a notice when binding fails.
  An explicit user choice of one-way delivery, however, needs no binding or ownership transfer.

## One-way notice

1. Require direct user authorization for this exact message and identify the exact existing target.
   Do not create a new task or broaden the recipient's work/publication authority.
2. Fresh-read the target with one `codex_app__wait_threads` snapshot (`timeoutMs: 0`, last known
   `afterCursor` when available). Use a bounded `codex_app__read_thread` only if needed; reconcile
   newer decisions and outcomes before composing. If inspection is unavailable, do not send.
3. Send the bounded notice once through `codex_app__send_message_to_thread`, without unrequested
   model or reasoning overrides. An accepted send is delivery evidence; it is not acceptance of
   any work mentioned in the notice. Optionally take one immediate compact snapshot, then report
   delivery and finish this message scope. Do not wait for a terminal result or promise a wake.

No source UUID or return binding is required for a notice. Do not call bind, confirm, rollback,
detach, transfer, or arm tools for it. Preserve existing parent ownership and return state;
an unrelated retained parent alone does not block the notice. If a known return is already armed
for another delegated turn, do not disarm it or claim this notice cannot affect its completion:
stop for that overlap rather than changing ownership or silently reusing the pending return.

## Delegated work with automatic return

The following binding workflow applies only to this mode, not to one-way notices.

1. Require direct user authorization for the exact cross-task message. Permission to do the
   underlying project work is not permission to steer another task.
2. Identify the exact target task. Create a new user-owned task only when the user explicitly asks
   for a new task; otherwise use the existing task they named.
3. Read the exact source task id from its own environment:

   ```powershell
   $env:CODEX_THREAD_ID
   ```

   Require one UUID-shaped value. If it is missing or invalid, stop before sending: the exact
   return route cannot be proven.
4. Fresh-read the target before composing the message:
   - call `codex_app__wait_threads` once with `timeoutMs: 0` and the last known `afterCursor` when
     one exists;
   - use `codex_app__read_thread` only when the compact snapshot is insufficient;
   - reconcile every newer user message, decision, assistant update, tool outcome, blocker, and
     status since the previous inspection;
   - retain the fresh target cursor returned by the wait.

   If fresh inspection is unavailable, do not send. Continue only independent local work.
5. Before sending, call `codex_bind_return` with:
   - the exact current workspace;
   - exact target task id;
   - exact source task id;
   - the fresh target cursor;
   - `transfer_parent: false` unless the user explicitly approved moving ownership.

   Require `state: bound`, `wake_on_completion: true`, `one_shot_return: true`,
   `return_state: prepared`, `completion_claimable: true`, and
   `persistent_parent_binding: true`. Persistent parent ownership and the one-shot return arm are
   separate: this bind prepares exactly one delegated turn without subscribing to every future
   turn of the target task.
   The bind result also contains the app-server baseline turn and its visible items. Reconcile
   those items with the fresh wait snapshot before sending; if they contain a newer user change
   that was not in the snapshot, refresh the target instead of sending from stale context.
6. Send the bounded follow-up with `codex_app__send_message_to_thread`. Do not add a model or
   reasoning override unless the user explicitly requested it.
7. Finish the prepared binding:
   - when the send is accepted and `binding_changed: true`, call `codex_confirm_return` with the
     returned binding id and rollback token, then require `return_state: armed`,
     `return_armed: true`, and `one_shot_return: true`;
   - when the send fails before a target turn starts and a rollback token exists, call
     `codex_rollback_return`;
   - an unchanged prepared or armed binding needs neither confirmation nor rollback. A consumed
     binding must never be reused as an armed return; a fresh explicit follow-up must produce a new
     prepared binding id.
8. Optionally take one immediate `codex_app__wait_threads` snapshot with `timeoutMs: 0` to confirm
   that the target accepted or started the turn. Do not keep waiting.
9. Report the submitted target task and end the source turn. PetCrew observes completion without a
   model call; the existing Relay resumes this exact source task.

## Returned turn

The Relay prompt contains the exact target task, source task, workspace, and terminal receipt. On
return:

1. Call `codex_read_return` once with the exact workspace, target task id, source task id, and
   completion id from the Relay prompt. Do not call Desktop-only `codex_app__wait_threads` or
   `codex_app__read_thread` from the headless resumed turn.
2. Require `matching_terminal_receipt: true`. `receipt_state: attempted_current_resume` is valid in
   this same resumed turn: Relay can mark it `delivered` only after the current `codex exec resume`
   process exits successfully.
   The wake itself is proven when the exact Relay user-message appears in the source task. Do not
   wait for the whole resumed model turn to finish before calling the wake successful; that turn may
   perform a long bounded reconciliation after it has already received the receipt.
3. Reconcile the returned full bounded delta before reporting, applying, or sending anything else.
4. Continue the user's existing source task from the verified result. Do not resend the delegated
   prompt and do not treat an arbitrary latest response as the matching completion.
5. If the next step would send, steer, detach, transfer, or otherwise mutate another Codex task,
   stop after reporting the verified result. A headless returned turn has no Desktop task tools and
   cannot obtain interactive approval for a mutating MCP call. Perform the next fresh-read -> bind
   -> send -> confirm workflow only from a later visible, user-authorized turn. Never disguise that
   mutation as read-only or add polling to simulate automatic multi-hop dispatch.

If the target completed unusually early while the source turn was still ending, Relay performs at
most one deferred retry when PetCrew emits the source turn's own terminal event. This is event-
driven and bounded; it is not a timer or polling loop.

## Persistent ownership

- One target task has one source task.
- A different source cannot take over silently. Set `transfer_parent: true` only after explicit
  user approval.
- Use `codex_detach_return` only when the user asks to disconnect the relationship.
- Do not try to detach from the headless returned turn. A persistent binding is intentional; when
  the user asks to disconnect it, detach in a later visible task turn where approval is available.
- Parent ownership remains available after a returned turn, but its one-shot arm is consumed before
  the source resume starts. Later manual target turns remain local and must not wake the source.
  Every new authorized automatic-return delegation requires a fresh-read and a new prepared ->
  armed cycle. A one-way notice never creates or changes that cycle.
- `codex_return_status` is tri-state. `bound: false` is authoritative only with `state: unbound`.
  Treat `bound: null`, `state_invalid`, `state_unavailable`, or `runtime_stale` as a status-surface
  failure, not as proof that the persistent binding was detached. Preserve the binding and inspect
  the returned runtime provenance instead of rebinding blindly.

## Parent transfer

Inspect the current source parent with `codex_get_return_parent`. Transfer to a new
source with `codex_transfer_return_parent` (requires `transfer_parent: true` and the
exact `expected_binding_id` CAS token). A transfer sets `return_state: parent_only`
and `return_armed: false` — parenthood alone never arms or emits a return. After
transfer, the normal bind -> send -> confirm workflow creates a fresh armed generation
from `parent_only` state. The transfer does not call the Codex reader, app-server,
inbox, or make any network request.

## Failure boundaries

- Never create a minute heartbeat, recurring automation, or model polling loop for return delivery.
- Never change or replace the user's existing Codex `notify` command for this bridge.
- For automatic-return delegation, never send first and bind afterward.
- Never steer from a stale snapshot.
- In automatic-return mode, treat a missing cursor, rejected binding, failed rollback,
  unconfirmed changed binding, or absent persistent-return flags as a bridge-contract failure.
  Tell the user the automatic return is not armed; do not silently switch delivery modes.
- A terminal receipt atomically consumes the one-shot arm before resume. The same receipt and later
  unarmed target turns cannot start the source again.
- `in_flight_completion_count > 0` indicates attempted receipts without confirmed delivery;
  it does not by itself prove a model turn is currently running. Reconcile exact execution
  evidence before manual wake, transfer or detach. A metadata transfer cannot undo a turn
  that already started, and must not reroute or replay an old-generation receipt.
