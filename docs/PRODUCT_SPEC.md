# Product specification

## Problem

The built-in pet is visually appealing but its activity UI prioritizes a small number of tasks. It does not provide a persistent, truthful view of every active agent or a semantic explanation of what each agent is doing.

PetCrew adds a local companion panel while preserving the existing pet as the visual anchor.

## Primary user outcome

At a glance, the user can answer:

- How many agents are running?
- Which provider and project does each agent belong to?
- What meaningful step is each agent performing now?
- Which agents need input or approval?
- Which agents finished, and what did they return?
- Can I open the relevant task or terminal directly?

## Display modes

### One to three agents

Show detailed cards with provider, task, progress, current action, elapsed time, and last result.

### Four to ten agents

Use compact rows. Keep progress, current action, and attention state visible. Expand a row on hover or click.

### More than ten agents

Group by project, provider, or state. Never hide the count of active or attention-requiring agents. Waiting, blocked, failed, and unread-completed agents remain individually accessible.

Essential card text remains readable at every density. Task text is at least 14 px, streamed action
text at least 12 px, and provider, phase, and progress labels at least 10 px. More cards are reached
by scrolling; PetCrew does not shrink essential text to fit the viewport.

Density and card layout are separate choices. Density still controls how much detail a card shows,
while the user explicitly selects either a vertical `Список` or a responsive `Плитки` layout.
`Плитки` places the same complete horizontal cards in two or three columns whenever the available
width supports their readable minimum; it does not force decorative square cards. Changing streamed
content must update a tile in place without changing the ordering rules above.

The user can select `Тёмная` or `Светлая` appearance and one of three text sizes with the titlebar
`A−` and `A+` controls. Text size changes typography only; native window size remains controlled by
mouse dragging and the visible resize grip. Defaults are large text, list layout, and dark theme.
The ordinary header exposes only the list/tile switch, one compact sun theme toggle, text-size
controls, and close. Demo source, fixture controls, and recent-result retention remain available
behind a small settings button instead of consuming permanent header space.

Every `working` card shows elapsed time since the current turn started, for example `7 мин` or
`1 ч 12 мин`. The clock updates locally at least once per minute and semantic activity updates do
not reset it. PetCrew uses an explicit hook start timestamp or the typed recovered `task_started`
timestamp; it does not invent a duration when neither exists.

Every terminal card with a valid event timestamp shows when the work ended. Today's result uses
`в 16:42`, yesterday's uses `вчера в 16:42`, and older retained results include day and month. Local
acknowledgement does not change this timestamp.

The native application opens on the live local-hub source. The live view never renders
`simulator` provider cards and contains no fixture/transport-demo controls. Fixtures remain
available only in the separate `Демо` source and never replace or masquerade as live agents.

On desktop startup, if Windows reports more than one monitor, PetCrew opens at the top-right of the
first non-primary monitor's work area with a small scale-aware margin. With one monitor it preserves
the normal platform placement and remains freely draggable and resizable.

After the user moves or resizes PetCrew, the application restores that valid window rectangle on
the next launch. If the saved monitor is gone or the rectangle is no longer visible, PetCrew falls
back to the secondary-monitor preference above instead of opening off-screen.

The titlebar exposes a keyboard-accessible return button next to `A− / A+`. It selects the connected
monitor with the smallest logical work area, moves PetCrew to that monitor's top-right default
position, and immediately persists the resulting valid rectangle. This is an explicit recovery
action for Windows display reconnects; it does not continuously override manual window placement.

The live view is user-global, not repository-scoped. It combines active root and stable-ID child
agents from every hook-enabled Codex project. Project labels distinguish groups without exposing
full paths. Reopening PetCrew restores fresh registry state without asking agents to report through
the model.

Tasks and child agents that were already active before PetCrew hooks were installed also appear as
clearly labelled recovered cards from a read-only local-index bootstrap. The user is not required to
recreate or restart those tasks.

Recovered cards show the latest short assistant status instead of the generic `Недавно активна`.
A still-open turn remains in the stable working group. `task_complete` means that the agent
finished its turn: the card becomes `completed` with an unread result that can be opened or
acknowledged in PetCrew. A newer turn that starts and completes between two discovery polls still
produces a new unread result; acknowledgement from an older turn cannot hide it. Large ignored
rollout records such as attached images cannot roll a visible working card back to an older start
time or prevent the latest completion from winning. `waiting_input` is reserved for an explicit
`request_user_input` form;
`waiting_approval` is reserved for an explicit permission request.

Every card shows its project name inside the card. When a local Codex technical thread id is
available, the whole card is keyboard- and pointer-activatable and opens the matching task through
the canonical `codex://threads/<thread-id>` deep link. An OpenCode card with a validated local
project directory opens that project through OpenCode Desktop's supported
`opencode://open-project?directory=...` deep link; this is project navigation, not an exact
session-resume promise. Unsupported cards remain visibly non-clickable.

## Agent states

- `queued` - known but not started.
- `planning` - preparing or declaring work.
- `working` - actively performing a step.
- `waiting_input` - requires a user answer or business decision.
- `waiting_approval` - requires an execution or access approval.
- `blocked` - cannot continue without a dependency or external change.
- `completed` - finished successfully; result may be unread.
- `failed` - ended with an error.
- `cancelled` - stopped intentionally.

## Ordering

Default attention order:

1. Attention required: waiting for approval or input, blocked, or failed with an unread result.
2. Newly completed or cancelled with an unread result.
3. Planning or working.
4. Queued.
5. Completed and acknowledged.

Within the attention group, the most recently updated card is first. This is the only live group
whose ordinary status updates may change relative order. A newly started planning/working turn is
inserted first inside the work group using its immutable `started_at`; later action, progress,
timer, and `updated_at` changes update that card in place without making it jump. Cards without a
valid start time use stable identity as a deterministic fallback. A transition into an unread
terminal result is an intentional move toward the top so a result cannot finish below the visible
viewport. Unread terminal results and completed history are newest first.

## Visibility and retention

The visible team is not capped at ten. PetCrew always shows every non-terminal agent and every
terminal result that is still unread. It then adds the newest acknowledged terminal results up to a
user-configurable history tail; the default tail is ten.

Recovered discovery queries consider 15 minutes of Codex activity. A non-terminal card missing from
one query remains visible through a 60-second grace, reset by rediscovery, so a transient index
snapshot cannot make work flicker. A recovered completed card does not share the short activity
window: unread results remain until acknowledged, acknowledged results participate in the
configured newest-results tail, and both have a hard seven-day limit.

The local store has a normal retained capacity of 100 agents. When it reaches that capacity, it
removes the oldest acknowledged terminal results first. Active, waiting, blocked, and unread
records are protected from eviction. If protected records alone exceed 100, PetCrew keeps and
shows them all and exposes the overflow count instead of silently hiding work.

The full protected team remains available even when the compact view visually prioritizes
attention. Large teams use a scrollable grouped-density list.

## Honest progress

PetCrew supports three progress modes:

- `steps`: explicit `current/total`, for example `4/10`.
- `activity`: a known phase or count without a reliable total.
- `indeterminate`: running duration and current semantic action only.

An inferred tool event may update `current_action`, but it must not create a fake step count.

Provider-reported turn changes are shown separately as `Изменено N файлов · +A −D`. This technical
summary never becomes a percentage or fraction and never changes card ordering. Missing telemetry
is simply omitted. PetCrew does not infer per-agent changes from a shared Git working tree.

## Pet behavior

The pet communicates the aggregate team state:

- calm working animation when agents are active;
- attention animation when any agent needs input or approval;
- success animation when unread results arrive;
- blocked/error animation when work fails;
- idle animation when no work is active.

Continuous motion must remain cheap and meaningful. A blurred layer must not animate every frame,
and indeterminate progress uses a calm static busy marker rather than a permanently travelling bar.
Small pet motion may continue through transform-only animation, while
`prefers-reduced-motion: reduce` disables all nonessential movement.

Selecting the pet expands or focuses the PetCrew panel. PetCrew must also work as an independently positioned overlay if reliable docking is unavailable.

The agent area scrolls independently with the mouse wheel and exposes a visible scrollbar. The
borderless window can be resized from a visible lower-right grip. The supported logical-size range
is 390x620 through 900x1000. Titlebar `A−` and `A+` controls change text size, not window geometry.

Presentation preferences are stored in a versioned
`%LOCALAPPDATA%\app.petcrew.overlay\settings.json`: text size, list/tile layout, dark/light theme,
recent completed-result limit, and the last valid window rectangle/monitor. Invalid or unknown
values fall back safely to defaults.

## Interaction scope by phase

### Read-only MVP

- View agents and status.
- Expand details.
- Acknowledge a result locally.
- Attempt a safe jump to the related task or terminal.

### Later, after separate security review

- Answer a structured question.
- Approve or deny a request.
- Steer, stop, or resume an agent.

## Acceptance criteria for the first useful version

- A deterministic fixture renders ten simultaneous agents without losing any.
- The view switches density correctly between detailed and compact layouts.
- Waiting and completed results remain visible until acknowledged.
- All non-terminal and unread terminal agents remain visible regardless of the recent-results setting.
- The acknowledged-result tail defaults to ten and can be changed locally.
- The store evicts only the oldest acknowledged terminal records at its normal capacity of 100.
- Protected records may exceed 100 and the interface reports that overflow honestly.
- Changing the recent-results setting repaints the list immediately without requiring hover or window movement.
- Every unread terminal result exposes a local `Прочитано` action in the progress row; it does not
  add a separate card row. Explicit terminal progress such as `11/11` remains visible beside it.
  The updated snapshot is visible immediately.
- The team header exposes `Прочитать все` for all unread terminal results and `Прочитать субагентов`
  for unread terminal results with a parent agent. Neither action changes active or waiting cards.
- Acknowledging a result updates the card in place and preserves the user's current scroll position.
- A long Codex turn remains working with its original start time after `task_started` falls outside
  the bounded recent-file window; only a typed `task_complete` ends it.
- A long team list scrolls by mouse wheel and by dragging a visible scrollbar.
- At the minimum window size, the scrollbar has a real movable thumb and reaches the final agent card.
- The overlay resizes from a visible corner grip; `A−` and `A+` change among three text sizes.
- List/tile layout and dark/light theme switch immediately and survive application restart.
- A valid saved window rectangle is restored; an unavailable monitor falls back on-screen.
- The titlebar return button moves PetCrew to the top-right of the smallest connected monitor and
  saves that position without restarting the application.
- Missing progress totals render without a percentage or fraction.
- Russian strings fit without truncating essential status text.
- The native application opens on live Codex data without requiring a source switch.
- Fresh agents from at least two different Codex projects restore after PetCrew is closed and reopened.
- Routine restoration and updates require no semantic reporter or model tool call.
- Task, action, phase, provider, and progress text meet the minimum card sizes defined above.
- The borderless overlay can be repositioned by dragging its titlebar or pet area.
- The overlay has an explicit keyboard-accessible close button.
- The pet's ears and head remain attached as one figure at every supported window size.
- The overlay works without modifying Codex or OpenCode configuration.
- Demo event identifiers remain unique after an application restart even when the local step counter restarts.
