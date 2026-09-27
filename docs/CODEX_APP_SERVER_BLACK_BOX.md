# Codex app-server black box

status: accepted implementation contract
updated_at: 2026-09-24T02:31:00+03:00

## Purpose

PetCrew Core keeps a small local, event-driven evidence trail for Codex Desktop
queue stalls and adjacent transcript-integrity failures. The recorder is a
diagnostic module, not a recovery controller: it observes already-written local
technical logs and never starts a model, polls Codex, restarts Desktop, kills a
process, or writes to Codex state.

## Inputs and trigger

- The source is the current Codex technical log database in `%USERPROFILE%\.codex`
  (`logs_*.sqlite` and its WAL), selected by the same latest-version rule used
  for other Codex state sources.
- A local filesystem notification for the database or WAL schedules one bounded
  incremental read after a short debounce. No timer runs while the files are
  unchanged.
- The durable cursor is the last inspected `logs.id`. Startup establishes the
  current maximum id as a baseline, so historical logs do not create a storm.
- A new diagnostic record is written only for an allowlisted technical signal:
  app-server request pressure, queue/admission rejection, app-server timeout,
  model-list child-exit timeout, a repeated missing tool-output integrity error,
  MCP initialization warning, or technical log discontinuity. The integrity
  record may retain only the canonical Codex thread id parsed from the fixed
  technical envelope; it never retains the tool call id or surrounding body.
- If Codex creates a newer numbered log database, the parent-directory event
  switches the recorder to it and establishes a fresh current baseline; PetCrew
  does not need a periodic version check.

## Stored record

The append-only JSONL record may contain only:

- schema version, event id, UTC time, fixed event code and severity;
- source log id, process UUID and technical target;
- allowlisted app-server method, if one can be parsed;
- counts for a bounded recent request-pressure window;
- for up to eight recently updated Codex tasks: task id, rollout file size,
  modification time, whether the file ends in a newline, and whether its final
  complete line is a JSON object with an envelope `type`.

It must not persist prompts, responses, transcript lines, log bodies, tool
arguments/results, credentials, environment values, filesystem contents, or
database pages. Transcript files and Codex databases are opened read-only.

## Output and retention

- Runtime location: `%LOCALAPPDATA%\app.petcrew.overlay\diagnostics\codex-appserver.jsonl`.
- Cursor location: the same directory, `codex-appserver-cursor.json`, with one
  previous valid sibling backup used only if replacement is interrupted.
- A stable event id deduplicates repeated notifications and replayed rows.
- The JSONL file rotates at 1 MiB; four previous generations are retained.
- Cursor replacement uses a sibling temporary file plus a last-known-good
  backup, so an interrupted Windows rename still leaves one readable cursor.

## Failure semantics

- Missing Codex files, a busy SQLite database, malformed rows, or a watcher
  error must not stop PetCrew Core.
- On a transient read failure the cursor is not advanced past unread rows.
- The module never repairs or deletes a transcript. It leaves enough metadata
  to distinguish pre-existing truncation from damage observed after a stall.

## Manual emergency recovery

The Black Box recorder remains observation-only. A separate, user-confirmed Monitor action
`Аварийно восстановить Codex` handles a Desktop that is already unresponsive. It must not
start automatically on a warning: model-list and MCP timeouts alone do not prove a particular
process is the blocker.

Before terminating anything, the action identifies exactly one installed Codex Desktop main
process and its registered Windows app identity, then saves a bounded diagnostic snapshot of
that process tree and recent sanitized Black Box event codes. The snapshot contains only PIDs,
parent PIDs, executable names, creation times, and fixed event metadata; it excludes process
command lines, prompts, transcripts, credentials and environment values. Failure to save the
snapshot stops recovery before any process termination.

On a manual recovery click, a bounded, read-only Windows Wait Chain Traversal probe may add
App Server thread IDs, waited-on process PIDs, wait durations, and cycle flags to that snapshot.
The probe runs in a separate helper process with a deadline; failure or timeout must not block
the base snapshot or recovery. A wait-chain edge alone does **not** prove a child is hung: the
server also legitimately waits for active tool shells. Do not auto-terminate a process on this
evidence alone. A child-first attempt is permitted only when the same Desktop-owned App Server
has a recent model-list child-exit timeout, WCT reports an actual cycle involving exactly one
direct child PID, and that PID's creation identity and parent are freshly rechecked. Terminate
only that PID, not the App Server or Desktop; if it exits, report a **targeted attempt**, not a
confirmed recovery. A second user click may use the whole-Desktop fallback if Codex is still
stuck. Without this complete evidence, retain the existing whole-Desktop fallback. A blocked
wait without a cycle never authorizes a child kill.

## Manual pressure inspection and failure evidence

Monitor exposes a separate read-only `Проверить нагрузку Codex` action. It identifies the
single Desktop-owned App Server and reports factual current counts only: all descendants,
direct children, direct-child executable-name groups, and the number of direct `node_repl.exe`
workers used as a visible runtime-cohort indicator. It also reports whether the sanitized
Black Box contains request pressure, an App Server timeout, or a model-list child timeout for
that exact App Server during the last ten minutes. It must not infer a fixed capacity limit,
start a model, poll in the background, or terminate/restart anything.

The same explicit inspection may count tool-host App Servers whose exact command is
`codex.exe app-server --listen stdio://`. These are separate from the Desktop-owned App Server
and the plugin App Server. A tool-host instance is **not** killable merely because it is old,
idle, slow, or numerous. It is eligible for the separate manual orphan cleanup only when its
recorded parent no longer exists, or the parent PID has been reused by a process created after
the tool App Server. If the recorded parent is still the same live `node_repl.exe`, the process
is owned and cleanup must refuse it.

Manual orphan cleanup must save a bounded identity-only snapshot, refresh the full process
inventory, and recheck the exact PID, parent PID, creation time, executable path, command line,
and orphan condition immediately before termination. It may then run fixed
`taskkill.exe /F /T /PID <exact-pid>` for that candidate only and retain bounded exit/stdout/stderr
evidence. It must never select by process name, age, CPU, model-list timeout, port, or wildcard;
must never target the Desktop-owned or plugin App Server; and must not restart Desktop. A live
parent, missing creation identity, ambiguous command, changed PID identity, or snapshot failure
blocks the action. No background cleanup or polling is allowed.

Pressure inspection must explain the next safe action instead of presenting an orphan count as
the recovery verdict. If a recent fixed `turn_input_integrity_error` identifies one or more
canonical thread ids, Monitor may show those ids and offer to open the task so Lisa can stop its
current turn in Codex. Opening a task is navigation only: Monitor does not claim external ownership
of the Desktop turn and does not send `turn/interrupt`. If no exact task is known but a recent
App Server timeout, queue rejection, model-list child-exit timeout, or request-pressure signal is
present, Monitor must say that orphan cleanup will not repair a live-parent App Server and point to
the explicit emergency recovery action when Codex is unresponsive. If proven orphans exist, the
separate guarded cleanup remains available. These states are mutually explained; `0` orphans is not
reported as a successful recovery.

The emergency snapshot stores the same bounded process summary. Wait-chain helper failures
must retain a fixed diagnostic reason code. Every attempted fixed `taskkill.exe` operation
must retain its exit code and at most 512 characters each of sanitized stdout and stderr in
the local restart journal. These fields may contain only output of that exact fixed executable;
they must not contain prompts, transcripts, arbitrary command bodies, credentials, or
environment values. A diagnostic failure remains a failure and never widens kill authority.
The recovery snapshot remains bounded to at most 2,048 Desktop descendants; the prior 512
ceiling was too close to the observed 482-process incident tree to preserve evidence reliably.

The current model-list timeout logs do **not** provide a child PID on their own. The bounded
WCT cycle and fresh process identity checks supply the additional evidence required above.
When the blocker cannot be identified or isolated, the
accepted fallback is to force-close the rechecked exact Codex Desktop process tree, wait until
the old Desktop and its App Server child exit, relaunch the registered app, and verify a new
Desktop main process and App Server child. This is a whole-Codex restart, not a claim to have
found or repaired a specific worker. The confirmation warns that unfinished turns may be
interrupted. Helper commands run without visible console windows; stage, outcome and snapshot
path are retained locally for diagnosing any failed recovery. No name-wide process kill or
second App Server is allowed.

Acceptance requires identity/snapshot/confirmation tests, a constrained native build, and one
live user-triggered check when no valuable turn is active. Build and process presence alone do
not prove recovery of the Desktop user flow.

The user-visible recovery ladder is therefore: inspect and open one evidenced looping task when
Codex still responds; clean only exact orphan tool servers when they exist; otherwise use the
snapshot-first whole-Desktop recovery after confirmation. Failure at any stage retains its bounded
journal evidence and never widens the process selection rule.

## Acceptance

1. A synthetic WAL/file notification plus one allowlisted technical row produces
   exactly one sanitized record.
2. Replaying the same row produces zero duplicates.
3. A row containing prompt-like text never copies that text to the black box.
4. Transcript probe tests preserve the source bytes exactly and report only
   bounded metadata.
5. No changed input means no read, no output, no process action, and no model use.
6. Drop/shutdown is bounded and leaves no worker running.
