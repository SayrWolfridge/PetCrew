import assert from "node:assert/strict"
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { test } from "node:test"
import * as pluginModule from "./petcrew.js"
import { PetCrewPlugin } from "./petcrew.js"

const { __test } = PetCrewPlugin

test("exports exactly one OpenCode plugin function", () => {
  assert.deepEqual(Object.keys(pluginModule), ["PetCrewPlugin"])
  assert.equal(typeof PetCrewPlugin, "function")
})

test("plugin initialization never waits for the server-backed bootstrap", async () => {
  const never = new Promise(() => {})
  const initialized = PetCrewPlugin({
    directory: "C:\\Work\\PetCrew",
    client: { session: { list: () => never } },
  })
  const hooks = await Promise.race([
    initialized,
    new Promise((_, reject) => setTimeout(() => reject(new Error("plugin init blocked")), 100)),
  ])
  assert.equal(typeof hooks.event, "function")
  assert.equal(typeof hooks.dispose, "function")
  await hooks.dispose()
})

function event(type, properties) {
  return { event: { type, properties } }
}

function completedAssistant(sessionID, id = "msg-assistant", parentID = "msg-user") {
  return event("message.updated", {
    info: {
      id,
      sessionID,
      role: "assistant",
      parentID,
      time: {
        created: Date.parse("2026-07-19T18:00:01.000Z"),
        completed: Date.parse("2026-07-19T18:00:02.000Z"),
      },
    },
  })
}

function harness() {
  const sent = []
  let tick = 0
  const now = () => new Date(Date.UTC(2026, 6, 19, 18, 0, tick++))
  const mapper = __test.makeMapper({ directory: "C:\\Work\\PetCrew" }, async (value) => sent.push(value), now)
  return { sent, mapper }
}

function harnessWithClient(client) {
  const sent = []
  let tick = 0
  const now = () => new Date(Date.UTC(2026, 6, 19, 18, 0, tick++))
  const mapper = __test.makeMapper({ directory: "C:\\Work\\PetCrew", client }, async (value) => sent.push(value), now)
  return { sent, mapper }
}

test("redacts sensitive titles and keeps the display project path empty", () => {
  assert.equal(__test.cleanText("api_key=secret", "Задача OpenCode", 120), "Задача OpenCode")
  assert.deepEqual(__test.projectFromDirectory("C:\\Work\\PetCrew"), {
    id: __test.opaqueId("c:\\work\\petcrew", "project:"),
    name: "PetCrew",
    path: null,
  })
})

test("adds only a valid local project navigation target", async () => {
  assert.deepEqual(__test.navigationFromDirectory("C:\\Work\\PetCrew"), {
    kind: "provider",
    label: "Открыть проект в OpenCode",
    target: "C:\\Work\\PetCrew",
  })
  assert.equal(__test.navigationFromDirectory(""), null)
  assert.equal(__test.navigationFromDirectory("C:\\secret=token"), null)

  const { sent, mapper } = harness()
  mapper.remember({ id: "root", title: "Задача", directory: "C:\\Work\\PetCrew" })
  await mapper.processEvent(event("session.status", { sessionID: "root", status: { type: "busy" } }))
  assert.deepEqual(sent[0].payload.navigation, {
    kind: "provider",
    label: "Открыть проект в OpenCode",
    target: "C:\\Work\\PetCrew",
  })
  assert.equal(sent[0].payload.project.path, null)
})

test("maps a working turn, permission, resolution, and completion", async () => {
  const { sent, mapper } = harness()
  await mapper.processEvent(event("session.created", { info: { id: "ses-root", title: "Проверить отчёт", directory: "C:\\Work\\PetCrew" } }))
  await mapper.processEvent(event("session.status", { sessionID: "ses-root", status: { type: "busy" } }))
  await mapper.processEvent(event("message.updated", {
    info: {
      id: "msg-user",
      sessionID: "ses-root",
      role: "user",
      time: { created: Date.parse("2026-07-19T18:00:00.000Z") },
    },
  }))
  await mapper.processEvent(event("permission.asked", { sessionID: "ses-root", permission: "bash", patterns: ["secret command"] }))
  await mapper.processEvent(event("permission.replied", { sessionID: "ses-root", reply: "once" }))
  await mapper.processEvent(completedAssistant("ses-root"))
  await mapper.processEvent(event("session.idle", { sessionID: "ses-root" }))

  assert.deepEqual(sent.map((item) => item.event_type), [
    "agent.started",
    "agent.attention_requested",
    "agent.attention_resolved",
    "agent.completed",
  ])
  assert.equal(sent[1].payload.attention.kind, "approval")
  assert.equal(sent[3].payload.result.unread, true)
  assert.equal(JSON.stringify(sent).includes("secret command"), false)
})

test("reuses the root card identity for a new turn in the same conversation", async () => {
  const { sent, mapper } = harness()
  mapper.remember({ id: "ses-root", title: "Задача", directory: "C:\\Work\\PetCrew" })
  await mapper.processEvent(event("session.status", { sessionID: "ses-root", status: { type: "busy" } }))
  await mapper.processEvent(completedAssistant("ses-root", "msg-first", "msg-user-first"))
  await mapper.processEvent(event("session.idle", { sessionID: "ses-root" }))
  await mapper.processEvent(event("session.status", { sessionID: "ses-root", status: { type: "busy" } }))
  assert.equal(sent[0].agent_id, sent[2].agent_id)
  assert.equal(sent[0].session_id, sent[2].session_id)
  assert.notEqual(sent[0].payload.started_at, sent[2].payload.started_at)
})

test("maps child sessions to the active parent card", async () => {
  const { sent, mapper } = harness()
  mapper.remember({ id: "root", title: "Главная", directory: "C:\\Work\\PetCrew" })
  mapper.remember({ id: "child", parentID: "root", title: "Помощник", directory: "C:\\Work\\PetCrew" })
  await mapper.processEvent(event("session.status", { sessionID: "root", status: { type: "busy" } }))
  await mapper.processEvent(event("session.status", { sessionID: "child", status: { type: "busy" } }))
  assert.equal(sent[1].parent_agent_id, sent[0].agent_id)
  assert.equal(sent[1].session_id, sent[0].session_id)
  assert.notEqual(sent[1].agent_id, sent[0].agent_id)
})

test("uses explicit todo counts without retaining todo text", async () => {
  const { sent, mapper } = harness()
  mapper.remember({ id: "root", title: "Задача", directory: "C:\\Work\\PetCrew" })
  await mapper.processEvent(event("todo.updated", {
    sessionID: "root",
    todos: [
      { content: "private first step", status: "completed" },
      { content: "private second step", status: "in_progress" },
    ],
  }))
  assert.deepEqual(sent[0].payload.progress, {
    kind: "steps",
    current: 1,
    total: 2,
    label: "План: 1 из 2",
    source: "explicit",
  })
  assert.equal(JSON.stringify(sent).includes("private"), false)
})

test("maps provider diff to counters without retaining file names or patches", async () => {
  const { sent, mapper } = harness()
  mapper.remember({ id: "root", title: "Задача", directory: "C:\\Work\\PetCrew" })
  await mapper.processEvent(event("session.diff", {
    sessionID: "root",
    diff: [
      { file: "private-a.txt", additions: 160, deletions: 0, patch: "secret-a" },
      { file: "private-b.txt", additions: 4, deletions: 2, patch: "secret-b" },
    ],
  }))
  assert.deepEqual(sent[0].payload.change_summary, {
    files: 2,
    additions: 164,
    deletions: 2,
    source: "provider",
  })
  assert.equal(JSON.stringify(sent).includes("private-a"), false)
  assert.equal(JSON.stringify(sent).includes("secret-a"), false)
})

test("omits malformed provider diff instead of inventing counters", async () => {
  const { sent, mapper } = harness()
  mapper.remember({ id: "root", title: "Задача", directory: "C:\\Work\\PetCrew" })
  await mapper.processEvent(event("session.diff", {
    sessionID: "root",
    diff: [{ additions: 4, deletions: -1 }],
  }))
  assert.equal(sent.length, 0)
})

test("fetches final SDK diff before completion and keeps counters on the terminal event", async () => {
  const calls = []
  const { sent, mapper } = harnessWithClient({ session: {
    diff: async (options) => {
      calls.push(options)
      return { data: [{ file: "private.md", additions: 101, deletions: 0, patch: "private patch" }] }
    },
  } })
  mapper.remember({ id: "root", title: "Задача", directory: "C:\\Work\\PetCrew" })
  await mapper.processEvent(event("session.status", { sessionID: "root", status: { type: "busy" } }))
  await mapper.processEvent(completedAssistant("root"))
  await mapper.processEvent(event("session.idle", { sessionID: "root" }))

  assert.deepEqual(calls, [{ path: { id: "root" } }])
  assert.equal(sent[1].event_type, "agent.completed")
  assert.deepEqual(sent[1].payload.change_summary, {
    files: 1,
    additions: 101,
    deletions: 0,
    source: "provider",
  })
  assert.equal(JSON.stringify(sent).includes("private.md"), false)
  assert.equal(JSON.stringify(sent).includes("private patch"), false)
})

test("final diff failure never blocks completion", async () => {
  const { sent, mapper } = harnessWithClient({ session: {
    diff: async () => { throw new Error("local server unavailable") },
  } })
  mapper.remember({ id: "root", title: "Задача", directory: "C:\\Work\\PetCrew" })
  await mapper.processEvent(event("session.status", { sessionID: "root", status: { type: "busy" } }))
  await mapper.processEvent(completedAssistant("root"))
  await mapper.processEvent(event("session.idle", { sessionID: "root" }))
  assert.equal(sent[1].event_type, "agent.completed")
})

test("does not complete when idle follows only a new user message", async () => {
  const { sent, mapper } = harness()
  mapper.remember({ id: "root", title: "Задача", directory: "C:\\Work\\PetCrew" })
  await mapper.processEvent(event("session.status", { sessionID: "root", status: { type: "busy" } }))
  await mapper.processEvent(event("message.updated", {
    info: {
      id: "msg-user-only",
      sessionID: "root",
      role: "user",
      time: { created: Date.parse("2026-07-19T18:00:00.000Z") },
    },
  }))
  await mapper.processEvent(event("session.idle", { sessionID: "root" }))

  assert.deepEqual(sent.map((item) => item.event_type), ["agent.started"])
})

test("a completed assistant message creates one deterministic terminal receipt", async () => {
  const first = harness()
  const second = harness()
  for (const { mapper } of [first, second]) {
    mapper.remember({ id: "root", title: "Задача", directory: "C:\\Work\\PetCrew" })
    await mapper.processEvent(event("session.status", { sessionID: "root", status: { type: "busy" } }))
    await mapper.processEvent(event("message.updated", {
      info: {
        id: "msg-user-stable",
        sessionID: "root",
        role: "user",
        time: { created: Date.parse("2026-07-19T18:00:00.000Z") },
      },
    }))
    await mapper.processEvent(completedAssistant("root", "msg-assistant-stable", "msg-user-stable"))
    await mapper.processEvent(event("session.idle", { sessionID: "root" }))
  }

  assert.equal(first.sent[1].event_type, "agent.completed")
  assert.equal(first.sent[1].event_id, second.sent[1].event_id)
  assert.match(first.sent[1].event_id, /^terminal:[0-9a-f]{64}$/)
  assert.equal(JSON.stringify(first.sent).includes("msg-assistant-stable"), false)
})

test("an assistant completed for an older user message cannot close the current turn", async () => {
  const { sent, mapper } = harness()
  mapper.remember({ id: "root", title: "Задача", directory: "C:\\Work\\PetCrew" })
  await mapper.processEvent(event("session.status", { sessionID: "root", status: { type: "busy" } }))
  await mapper.processEvent(event("message.updated", {
    info: {
      id: "msg-current-user",
      sessionID: "root",
      role: "user",
      time: { created: Date.parse("2026-07-19T18:00:00.000Z") },
    },
  }))
  await mapper.processEvent(completedAssistant("root", "msg-old-assistant", "msg-old-user"))
  await mapper.processEvent(event("session.idle", { sessionID: "root" }))

  assert.deepEqual(sent.map((item) => item.event_type), ["agent.started"])
})

test("does not surface historical idle sessions during bootstrap", async () => {
  const { sent, mapper } = harness()
  await mapper.bootstrap({ session: {
    list: async () => ({ data: [{ id: "old", title: "Старая", directory: "C:\\Old" }] }),
    status: async () => ({ data: { old: { type: "idle" } } }),
  } })
  assert.equal(sent.length, 0)
})

test("recovers only currently busy sessions through the official client", async () => {
  const { sent, mapper } = harness()
  await mapper.bootstrap({ session: {
    list: async () => ({ data: [
      { id: "busy", title: "Работа", directory: "C:\\Work\\A" },
      { id: "idle", title: "История", directory: "C:\\Work\\B" },
    ] }),
    status: async () => ({ data: { busy: { type: "busy" }, idle: { type: "idle" } } }),
  } })
  assert.equal(sent.length, 1)
  assert.equal(sent[0].event_type, "agent.started")
  assert.equal(sent[0].payload.task.title, "Работа")
})

test("repairs one missed CLI completion from a fresh persisted working root", async () => {
  const sent = []
  const listCalls = []
  const now = () => new Date("2026-07-19T18:00:00.000Z")
  const recovered = []
  const mapper = __test.makeMapper(
    { directory: "C:\\Work\\PetCrew" },
    async (value) => sent.push(value),
    now,
    async (sessionID) => {
      recovered.push(sessionID)
      return sessionID === "idle-live"
        ? { startedAt: "2026-07-19T17:45:00.000Z" }
        : null
    },
  )
  await mapper.bootstrap({ session: {
    list: async (parameters) => {
      listCalls.push(parameters)
      return { data: [
        { id: "idle-live", title: "CLI завершён", directory: "C:\\Work\\PetCrew", time: { updated: 2 } },
        { id: "idle-history", title: "Старая история", directory: "C:\\Work\\PetCrew", time: { updated: 1 } },
      ] }
    },
    status: async () => ({ data: {} }),
    messages: async ({ path }) => ({
      data: path.id === "idle-live"
        ? [
            {
              info: {
                id: "msg-user-recovered",
                sessionID: "idle-live",
                role: "user",
                time: { created: Date.parse("2026-07-19T17:50:00.000Z") },
              },
              parts: [{ type: "text", text: "must stay ignored" }],
            },
            {
              info: {
                id: "msg-assistant-recovered",
                sessionID: "idle-live",
                role: "assistant",
                parentID: "msg-user-recovered",
                time: {
                  created: Date.parse("2026-07-19T17:55:00.000Z"),
                  completed: Date.parse("2026-07-19T17:59:00.000Z"),
                },
              },
              parts: [{ type: "text", text: "must stay ignored" }],
            },
          ]
        : [],
    }),
  } })

  assert.deepEqual(listCalls, [{
    query: { directory: "", roots: true, limit: 200 },
  }])
  assert.deepEqual(recovered, ["idle-live", "idle-history"])
  assert.equal(sent.length, 1)
  assert.equal(sent[0].event_type, "agent.completed")
  assert.equal(sent[0].agent_id, __test.stableRootIdentity("idle-live").agent_id)
  assert.equal(sent[0].payload.started_at, "2026-07-19T17:45:00.000Z")
  assert.equal(sent[0].payload.result.summary, "Закончил работу")
})

test("accepts only a fresh sanitized working root for CLI completion recovery", () => {
  const now = () => new Date("2026-07-19T18:00:00.000Z")
  const identity = __test.stableRootIdentity("ses-live")
  const active = {
    protocol_version: "1.0",
    provider: "opencode",
    session_id: identity.session_id,
    agent_id: identity.agent_id,
    parent_agent_id: null,
    event_type: "agent.activity",
    occurred_at: "2026-07-19T17:55:00.000Z",
    payload: {
      phase: "working",
      started_at: "2026-07-19T17:30:00.000Z",
    },
  }
  assert.deepEqual(__test.recoverableActiveRoot(active, identity, now), {
    startedAt: "2026-07-19T17:30:00.000Z",
  })

  const waiting = structuredClone(active)
  waiting.payload.phase = "waiting_input"
  assert.equal(__test.recoverableActiveRoot(waiting, identity, now), null)

  const terminal = structuredClone(active)
  terminal.event_type = "agent.completed"
  terminal.payload.phase = "completed"
  assert.equal(__test.recoverableActiveRoot(terminal, identity, now), null)

  const stale = structuredClone(active)
  stale.occurred_at = "2026-07-18T17:00:00.000Z"
  assert.equal(__test.recoverableActiveRoot(stale, identity, now), null)

  const wrongIdentity = structuredClone(active)
  wrongIdentity.session_id = "session:visible-path"
  assert.equal(__test.recoverableActiveRoot(wrongIdentity, identity, now), null)
})

test("a terminal registry replacement prevents duplicate CLI recovery", async () => {
  const root = await mkdtemp(join(tmpdir(), "petcrew-opencode-recovery-"))
  const now = () => new Date("2026-07-19T18:00:00.000Z")
  try {
    const runtime = __test.createRuntime({ dataDir: root, now })
    const identity = __test.stableRootIdentity("ses-recovery")
    const base = {
      protocol_version: "1.0",
      event_id: "recovery-working",
      sequence: 0,
      occurred_at: "2026-07-19T17:55:00.000Z",
      provider: "opencode",
      ...identity,
      event_type: "agent.activity",
      payload: {
        phase: "working",
        started_at: "2026-07-19T17:30:00.000Z",
      },
    }
    await runtime.deliver(base)
    assert.deepEqual(await runtime.recoverActiveRoot("ses-recovery"), {
      startedAt: "2026-07-19T17:30:00.000Z",
    })

    await runtime.deliver({
      ...base,
      event_id: "recovery-completed",
      event_type: "agent.completed",
      occurred_at: "2026-07-19T18:00:00.000Z",
      payload: {
        phase: "completed",
        started_at: "2026-07-19T17:30:00.000Z",
        result: {
          summary: "Закончил работу",
          outcome: "success",
          completed_at: "2026-07-19T18:00:00.000Z",
          unread: true,
        },
      },
    })
    assert.equal(await runtime.recoverActiveRoot("ses-recovery"), null)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test("maps only the tool name to a generic action", async () => {
  const { sent, mapper } = harness()
  mapper.remember({ id: "root", title: "Задача", directory: "C:\\Work\\PetCrew" })
  await mapper.toolBefore({ sessionID: "root", tool: "bash", args: { command: "secret-command" } })
  assert.equal(sent[0].payload.current_action, "Работает в терминале")
  assert.equal(JSON.stringify(sent).includes("secret-command"), false)
})

test("surfaces a live session error without retaining its body", async () => {
  const { sent, mapper } = harness()
  mapper.remember({ id: "root", title: "Задача", directory: "C:\\Work\\PetCrew" })
  await mapper.processEvent(event("session.error", {
    sessionID: "root",
    error: { data: { message: "api_key=very-secret" } },
  }))
  assert.equal(sent[0].event_type, "agent.failed")
  assert.equal(sent[0].payload.result.summary, "Работа завершилась с ошибкой")
  assert.equal(JSON.stringify(sent).includes("very-secret"), false)
})

test("checks local hub health before posting an event", async () => {
  const root = await mkdtemp(join(tmpdir(), "petcrew-opencode-test-"))
  try {
    const secret = "a".repeat(64)
    const secretPath = join(root, "hub-secret.txt")
    await mkdir(root, { recursive: true })
    await writeFile(secretPath, secret, "utf8")
    await writeFile(join(root, "hub-runtime.json"), JSON.stringify({
      protocol_version: "1.0",
      endpoint: "http://127.0.0.1:43123",
      secret_file: secretPath,
    }), "utf8")
    const calls = []
    const fetchImpl = async (url, options) => {
      calls.push({ url, options })
      return { status: calls.length === 1 ? 200 : 202 }
    }
    const accepted = await __test.postEvent(root, { event_id: "event" }, fetchImpl)
    assert.equal(accepted, true)
    assert.equal(calls[0].url, "http://127.0.0.1:43123/health")
    assert.equal(calls[0].options.method, "GET")
    assert.equal(calls[1].url, "http://127.0.0.1:43123/v1/events")
    assert.equal(calls[1].options.headers.Authorization, `Bearer ${secret}`)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test("does not post when local hub health fails", async () => {
  const root = await mkdtemp(join(tmpdir(), "petcrew-opencode-test-"))
  try {
    const secretPath = join(root, "hub-secret.txt")
    await writeFile(secretPath, "b".repeat(64), "utf8")
    await writeFile(join(root, "hub-runtime.json"), JSON.stringify({
      protocol_version: "1.0",
      endpoint: "http://127.0.0.1:43124",
      secret_file: secretPath,
    }), "utf8")
    let calls = 0
    const accepted = await __test.postEvent(root, { event_id: "event" }, async () => {
      calls += 1
      return { status: 503 }
    })
    assert.equal(accepted, false)
    assert.equal(calls, 1)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
