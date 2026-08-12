import { createHash, randomUUID } from "node:crypto"
import { mkdir, readFile, rename, stat, unlink, writeFile } from "node:fs/promises"
import { basename, dirname, isAbsolute, join, relative, resolve } from "node:path"

const PROTOCOL_VERSION = "1.0"
const PROVIDER = "opencode"
const APP_DATA_FOLDER = "app.petcrew.overlay"
const REGISTRY_FOLDER = "agent-registry"
const MAX_RUNTIME_BYTES = 8_192
const MAX_SECRET_BYTES = 256
const MAX_EVENT_BYTES = 64 * 1024
const POST_TIMEOUT_MS = 250
const ACTIVE_RECOVERY_TTL_MS = 24 * 60 * 60 * 1000
const MAX_BOOTSTRAP_SESSIONS = 200
const RECOVERABLE_ACTIVE_EVENT_TYPES = new Set([
  "agent.started",
  "agent.progress",
  "agent.activity",
  "agent.attention_resolved",
])
const SENSITIVE_MARKERS = [
  "bearer ",
  "password=",
  "password:",
  "api_key=",
  "apikey=",
  "access_token=",
  "secret=",
  "sk-",
]

let lastSequence = 0

function containsSensitiveMarker(value) {
  const lower = String(value).toLowerCase()
  return SENSITIVE_MARKERS.some((marker) => lower.includes(marker))
}

function cleanText(value, fallback, limit) {
  if (typeof value !== "string") return fallback
  const cleaned = value.replace(/\s+/g, " ").trim()
  if (!cleaned || containsSensitiveMarker(cleaned)) return fallback
  return cleaned.slice(0, limit)
}

function opaqueId(value, prefix) {
  if (typeof value !== "string" || !value.trim() || containsSensitiveMarker(value)) return ""
  return `${prefix}${createHash("sha256").update(value.trim(), "utf8").digest("hex")}`
}

function terminalEventId(rawSessionId, assistantMessageId, phase = "completed") {
  if (
    typeof rawSessionId !== "string"
    || !rawSessionId.trim()
    || typeof assistantMessageId !== "string"
    || !assistantMessageId.trim()
  ) return ""
  const receipt = `${rawSessionId.trim()}\0${assistantMessageId.trim()}\0${phase}`
  return `terminal:${createHash("sha256").update(receipt, "utf8").digest("hex")}`
}

function isoTime(value = Date.now()) {
  const date = value instanceof Date ? value : new Date(value)
  return Number.isNaN(date.getTime()) ? new Date().toISOString() : date.toISOString()
}

function projectFromDirectory(directory) {
  if (typeof directory !== "string" || !directory.trim() || containsSensitiveMarker(directory)) {
    return { id: "project:unknown", name: "Проект OpenCode", path: null }
  }
  const resolvedDirectory = resolve(directory.trim())
  const normalized = resolvedDirectory.toLowerCase()
  return {
    id: opaqueId(normalized, "project:"),
    name: cleanText(basename(resolvedDirectory), "Проект OpenCode", 120),
    path: null,
  }
}

function navigationFromDirectory(directory) {
  if (typeof directory !== "string" || !directory.trim() || containsSensitiveMarker(directory)) {
    return null
  }
  const resolvedDirectory = resolve(directory.trim())
  if (!isAbsolute(resolvedDirectory)) return null
  return {
    kind: "provider",
    label: "Открыть проект в OpenCode",
    target: resolvedDirectory,
  }
}

function genericToolAction(tool) {
  const normalized = typeof tool === "string" ? tool.toLowerCase() : ""
  if (["bash", "shell", "terminal"].some((name) => normalized.includes(name))) return "Работает в терминале"
  if (["edit", "write", "patch"].some((name) => normalized.includes(name))) return "Изменяет файлы"
  if (["web", "browser", "fetch"].some((name) => normalized.includes(name))) return "Проверяет в браузере"
  if (["task", "agent"].some((name) => normalized.includes(name))) return "Координирует помощника"
  if (["todo", "plan"].some((name) => normalized.includes(name))) return "Обновляет план"
  if (["read", "grep", "glob", "list"].some((name) => normalized.includes(name))) return "Изучает материалы"
  return "Выполняет действие"
}

function changeSummary(diff) {
  if (!Array.isArray(diff)) return null
  let additions = 0
  let deletions = 0
  for (const file of diff) {
    const added = Number(file?.additions)
    const deleted = Number(file?.deletions)
    if (!Number.isSafeInteger(added) || added < 0 || !Number.isSafeInteger(deleted) || deleted < 0) {
      return null
    }
    additions += added
    deletions += deleted
  }
  if (!Number.isSafeInteger(additions) || !Number.isSafeInteger(deletions)) return null
  return { files: diff.length, additions, deletions, source: "provider" }
}

function result(summary, outcome, completedAt) {
  return { summary, outcome, completed_at: completedAt, unread: true }
}

function attention(kind, summary, requestedAt) {
  return { kind, summary, requested_at: requestedAt }
}

function dataRoot(override) {
  if (override) return resolve(override)
  if (process.env.PETCREW_DATA_DIR) return resolve(process.env.PETCREW_DATA_DIR)
  if (!process.env.LOCALAPPDATA) return null
  return join(process.env.LOCALAPPDATA, APP_DATA_FOLDER)
}

function pathIsInside(child, parent) {
  const rel = relative(resolve(parent), resolve(child))
  return rel === "" || (!rel.startsWith("..") && !isAbsolute(rel))
}

async function readSmallJson(path, maxBytes) {
  const info = await stat(path)
  if (info.size > maxBytes) throw new Error("file-too-large")
  return JSON.parse(await readFile(path, "utf8"))
}

async function discoverConnection(root) {
  if (!root) return null
  try {
    const descriptor = await readSmallJson(join(root, "hub-runtime.json"), MAX_RUNTIME_BYTES)
    if (descriptor?.protocol_version !== PROTOCOL_VERSION) return null
    if (typeof descriptor.endpoint !== "string" || typeof descriptor.secret_file !== "string") return null
    const endpoint = new URL(descriptor.endpoint)
    if (
      endpoint.protocol !== "http:" ||
      !["127.0.0.1", "localhost", "::1", "[::1]"].includes(endpoint.hostname) ||
      !endpoint.port || endpoint.username || endpoint.password || endpoint.search || endpoint.hash ||
      !["", "/"].includes(endpoint.pathname)
    ) return null
    const secretPath = resolve(descriptor.secret_file)
    if (!pathIsInside(secretPath, root)) return null
    const secretInfo = await stat(secretPath)
    if (secretInfo.size > MAX_SECRET_BYTES) return null
    const token = (await readFile(secretPath, "utf8")).trim()
    if (!/^[0-9a-fA-F]{64}$/.test(token)) return null
    return { endpoint: endpoint.origin, token }
  } catch {
    return null
  }
}

function registryPath(root, event) {
  const key = [event.provider, event.session_id, event.agent_id].join("\0")
  return join(root, REGISTRY_FOLDER, `${createHash("sha256").update(key).digest("hex")}.json`)
}

function stableRootIdentity(rawSessionId) {
  const sessionId = opaqueId(rawSessionId, "session:")
  if (!sessionId) return null
  return {
    provider: PROVIDER,
    session_id: sessionId,
    agent_id: `root:${sessionId.slice("session:".length)}`,
    parent_agent_id: null,
  }
}

function validOpaqueIdentity(value, prefix) {
  if (typeof value !== "string" || !value.startsWith(prefix)) return false
  const digest = value.slice(prefix.length)
  return /^[0-9a-f]{64}$/.test(digest)
}

function recoverableActiveRoot(value, identity, now) {
  if (!value || typeof value !== "object" || !identity) return null
  if (
    value.protocol_version !== PROTOCOL_VERSION ||
    value.provider !== PROVIDER ||
    value.session_id !== identity.session_id ||
    value.agent_id !== identity.agent_id ||
    value.parent_agent_id != null ||
    !validOpaqueIdentity(value.session_id, "session:") ||
    !validOpaqueIdentity(value.agent_id, "root:") ||
    !RECOVERABLE_ACTIVE_EVENT_TYPES.has(value.event_type) ||
    value.payload?.phase !== "working" ||
    value.payload?.result != null
  ) return null
  const occurredAt = Date.parse(value.occurred_at)
  const startedAt = Date.parse(value.payload?.started_at)
  const currentTime = now().getTime()
  if (
    !Number.isFinite(occurredAt) ||
    !Number.isFinite(startedAt) ||
    occurredAt > currentTime + 5 * 60 * 1000 ||
    currentTime - occurredAt > ACTIVE_RECOVERY_TTL_MS ||
    startedAt > occurredAt + 5 * 60 * 1000
  ) return null
  return { startedAt: value.payload.started_at }
}

async function readPersistedActiveRoot(root, rawSessionId, now) {
  if (!root) return null
  const identity = stableRootIdentity(rawSessionId)
  if (!identity) return null
  try {
    const value = await readSmallJson(registryPath(root, identity), MAX_EVENT_BYTES)
    return recoverableActiveRoot(value, identity, now)
  } catch {
    return null
  }
}

async function previousSequence(path) {
  try {
    const value = await readSmallJson(path, MAX_EVENT_BYTES)
    return Number.isSafeInteger(value?.sequence) ? value.sequence : 0
  } catch {
    return 0
  }
}

async function persistEvent(root, eventWithoutSequence, now, uuid) {
  if (!root) return null
  try {
    const registry = join(root, REGISTRY_FOLDER)
    await mkdir(registry, { recursive: true })
    const target = registryPath(root, eventWithoutSequence)
    const persisted = await previousSequence(target)
    const clock = Math.floor(now().getTime() * 1_000)
    lastSequence = Math.max(lastSequence + 1, persisted + 1, clock)
    const event = { ...eventWithoutSequence, sequence: lastSequence }
    const body = JSON.stringify(event)
    if (Buffer.byteLength(body, "utf8") > MAX_EVENT_BYTES) return null
    const temporary = join(dirname(target), `.${basename(target)}.${process.pid}.${uuid()}.tmp`)
    try {
      await writeFile(temporary, body, { encoding: "utf8", flag: "wx" })
      await rename(temporary, target)
    } finally {
      await unlink(temporary).catch(() => {})
    }
    return event
  } catch {
    return null
  }
}

async function postEvent(root, event, fetchImpl) {
  const connection = await discoverConnection(root)
  if (!connection || !event) return false

  const healthController = new AbortController()
  const healthTimer = setTimeout(() => healthController.abort(), POST_TIMEOUT_MS)
  healthTimer.unref?.()
  try {
    const health = await fetchImpl(`${connection.endpoint}/health`, {
      method: "GET",
      signal: healthController.signal,
    })
    if (health.status !== 200) return false
  } catch {
    return false
  } finally {
    clearTimeout(healthTimer)
  }

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), POST_TIMEOUT_MS)
  timer.unref?.()
  try {
    const response = await fetchImpl(`${connection.endpoint}/v1/events`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${connection.token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(event),
      signal: controller.signal,
    })
    return response.status === 202
  } catch {
    return false
  } finally {
    clearTimeout(timer)
  }
}

function unwrapData(value) {
  return value?.data ?? value
}

function latestCompletedAssistant(messages, startedAt) {
  if (!Array.isArray(messages)) return null
  const startedMs = Date.parse(startedAt)
  if (!Number.isFinite(startedMs)) return null
  const infos = messages
    .map((message) => message?.info ?? message)
    .filter((info) => info && typeof info === "object")
  const users = infos
    .filter((info) => info.role === "user"
      && typeof info.id === "string"
      && Number.isFinite(Number(info.time?.created))
      && Number(info.time.created) >= startedMs)
    .sort((left, right) => Number(right.time.created) - Number(left.time.created))
  const latestUser = users[0] ?? null
  const assistants = infos
    .filter((info) => info.role === "assistant"
      && typeof info.id === "string"
      && Number.isFinite(Number(info.time?.created))
      && Number.isFinite(Number(info.time?.completed))
      && Number(info.time.created) >= startedMs
      && (
        latestUser === null
        || (
          info.parentID === latestUser.id
          && Number(info.time.created) >= Number(latestUser.time.created)
        )
      ))
    .sort((left, right) => Number(right.time.completed) - Number(left.time.completed))
  const assistant = assistants[0]
  if (!assistant) return null
  return {
    messageId: assistant.id,
    completedAt: isoTime(Number(assistant.time.completed)),
  }
}

function makeMapper(
  context,
  sink,
  now = () => new Date(),
  recoverActiveRoot = async () => null,
) {
  const sessions = new Map()
  const activeTurns = new Map()
  const phases = new Map()
  const changeSummaries = new Map()
  const currentUsers = new Map()
  const assistantReceipts = new Map()
  let turnCounter = 0

  function remember(info) {
    if (info && typeof info.id === "string") sessions.set(info.id, info)
  }

  function rootSessionId(rawSessionId) {
    let current = rawSessionId
    const visited = new Set()
    for (let depth = 0; depth < 20; depth += 1) {
      if (!current || visited.has(current)) break
      visited.add(current)
      const parent = sessions.get(current)?.parentID
      if (typeof parent !== "string" || !parent) break
      current = parent
    }
    return current || rawSessionId
  }

  function ensureTurn(rawSessionId, timestamp) {
    if (!activeTurns.has(rawSessionId)) {
      turnCounter += 1
      const nonce = `${rawSessionId}\0${timestamp}\0${turnCounter}`
      activeTurns.set(rawSessionId, {
        agentId: opaqueId(nonce, "turn:"),
        startedAt: timestamp,
      })
    }
    return activeTurns.get(rawSessionId)
  }

  function rootAgentId(rawSessionId) {
    const sessionId = opaqueId(rootSessionId(rawSessionId), "session:")
    return `root:${sessionId.slice("session:".length)}`
  }

  function identity(rawSessionId, timestamp) {
    const info = sessions.get(rawSessionId) ?? { id: rawSessionId, directory: context.directory }
    const turn = ensureTurn(rawSessionId, timestamp)
    const root = rootSessionId(rawSessionId)
    const isChild = typeof info.parentID === "string" && info.parentID
    let parent = null
    if (isChild) {
      const parentInfo = sessions.get(info.parentID)
      parent = parentInfo?.parentID
        ? ensureTurn(info.parentID, timestamp).agentId
        : rootAgentId(info.parentID)
    }
    return {
      session_id: opaqueId(root, "session:"),
      agent_id: isChild ? turn.agentId : rootAgentId(rawSessionId),
      parent_agent_id: parent,
      info,
      turn,
    }
  }

  function basePayload(rawSessionId, phase, action, timestamp) {
    const { info, turn } = identity(rawSessionId, timestamp)
    const isChild = typeof info.parentID === "string" && info.parentID
    const directory = info.directory ?? context.directory
    const payload = {
      project: projectFromDirectory(directory),
      task: {
        title: cleanText(info.title, isChild ? "Помощник OpenCode" : "Задача OpenCode", 120),
        detail: null,
      },
      phase,
      current_action: action,
      started_at: turn.startedAt,
    }
    const navigation = navigationFromDirectory(directory)
    if (navigation) payload.navigation = navigation
    const summary = changeSummaries.get(rawSessionId)
    if (summary) payload.change_summary = summary
    return payload
  }

  async function refreshFinalDiff(rawSessionId) {
    if (typeof rawSessionId !== "string" || !rawSessionId || !context.client?.session?.diff) return null
    try {
      const diff = unwrapData(await context.client.session.diff({ path: { id: rawSessionId } }))
      const summary = changeSummary(diff)
      if (summary) changeSummaries.set(rawSessionId, summary)
      return summary
    } catch {
      return null
    }
  }

  async function emit(rawSessionId, eventType, payload, timestamp, eventId = null) {
    if (typeof rawSessionId !== "string" || !rawSessionId) return
    const ids = identity(rawSessionId, timestamp)
    await sink({
      protocol_version: PROTOCOL_VERSION,
      event_id: eventId || randomUUID(),
      sequence: 0,
      occurred_at: timestamp,
      provider: PROVIDER,
      session_id: ids.session_id,
      agent_id: ids.agent_id,
      parent_agent_id: ids.parent_agent_id,
      event_type: eventType,
      payload,
    })
  }

  async function working(rawSessionId, action, timestamp, forceStart = false) {
    const previous = phases.get(rawSessionId)
    const eventType = forceStart || previous !== "working" ? "agent.started" : "agent.activity"
    phases.set(rawSessionId, "working")
    await emit(rawSessionId, eventType, basePayload(rawSessionId, "working", action, timestamp), timestamp)
  }

  async function terminal(
    rawSessionId,
    phase,
    summary,
    outcome,
    eventType,
    timestamp,
    receipt = null,
  ) {
    if (!activeTurns.has(rawSessionId)) return
    if (phase === "completed" && !receipt) return
    const payload = basePayload(rawSessionId, phase, summary, timestamp)
    payload.result = result(summary, outcome, timestamp)
    const eventId = phase === "completed"
      ? terminalEventId(rawSessionId, receipt.messageId, phase)
      : null
    if (phase === "completed" && !eventId) return
    await emit(rawSessionId, eventType, payload, timestamp, eventId)
    phases.set(rawSessionId, phase)
    activeTurns.delete(rawSessionId)
    changeSummaries.delete(rawSessionId)
    currentUsers.delete(rawSessionId)
    assistantReceipts.delete(rawSessionId)
  }

  function rememberMessage(info, timestamp) {
    if (
      !info
      || typeof info !== "object"
      || typeof info.id !== "string"
      || typeof info.sessionID !== "string"
    ) return
    const rawSessionId = info.sessionID
    if (info.role === "user") {
      const previous = currentUsers.get(rawSessionId)
      if (previous?.messageId !== info.id) assistantReceipts.delete(rawSessionId)
      ensureTurn(rawSessionId, timestamp)
      currentUsers.set(rawSessionId, {
        messageId: info.id,
        createdAt: Number(info.time?.created),
      })
      return
    }
    if (
      info.role !== "assistant"
      || !Number.isFinite(Number(info.time?.created))
      || !Number.isFinite(Number(info.time?.completed))
    ) return
    const turn = activeTurns.get(rawSessionId)
    if (!turn) return
    const user = currentUsers.get(rawSessionId)
    if (user && info.parentID !== user.messageId) return
    if (
      user
      && Number.isFinite(user.createdAt)
      && Number(info.time.created) < user.createdAt
    ) return
    if (!user && Number(info.time.created) < Date.parse(turn.startedAt)) return
    assistantReceipts.set(rawSessionId, {
      messageId: info.id,
      completedAt: isoTime(Number(info.time.completed)),
    })
  }

  async function recoverAssistantReceipt(rawSessionId, startedAt, client) {
    if (typeof client?.session?.messages !== "function") return null
    try {
      const messages = unwrapData(await client.session.messages({
        path: { id: rawSessionId },
        query: { limit: 20 },
      }))
      return latestCompletedAssistant(messages, startedAt)
    } catch {
      return null
    }
  }

  async function processEvent(envelope) {
    const event = envelope?.event ?? envelope
    const type = event?.type
    const properties = event?.properties ?? {}
    const timestamp = isoTime(now())
    const sessionID = properties.sessionID ?? properties.info?.id

    if (type === "session.created" || type === "session.updated") {
      remember(properties.info)
      return
    }
    if (type === "message.updated") {
      rememberMessage(properties.info, timestamp)
      return
    }
    if (type === "session.status") {
      if (properties.status?.type === "busy") await working(sessionID, "Работает над задачей", timestamp)
      else if (properties.status?.type === "retry") await working(sessionID, "Повторяет попытку", timestamp)
      else if (properties.status?.type === "idle") {
        const receipt = assistantReceipts.get(sessionID)
        await refreshFinalDiff(sessionID)
        await terminal(
          sessionID,
          "completed",
          "Закончил работу",
          "success",
          "agent.completed",
          receipt?.completedAt ?? timestamp,
          receipt,
        )
      }
      return
    }
    if (type === "session.idle") {
      const receipt = assistantReceipts.get(sessionID)
      await refreshFinalDiff(sessionID)
      await terminal(
        sessionID,
        "completed",
        "Закончил работу",
        "success",
        "agent.completed",
        receipt?.completedAt ?? timestamp,
        receipt,
      )
      return
    }
    if (type === "session.error") {
      ensureTurn(sessionID, timestamp)
      await terminal(sessionID, "failed", "Работа завершилась с ошибкой", "failure", "agent.failed", timestamp)
      return
    }
    if (type === "session.deleted") {
      remember(properties.info)
      await terminal(sessionID, "cancelled", "Работа отменена", "cancelled", "agent.cancelled", timestamp)
      return
    }
    if (["permission.asked", "permission.v2.asked"].includes(type)) {
      phases.set(sessionID, "waiting_approval")
      const payload = basePayload(sessionID, "waiting_approval", "Ждёт подтверждения", timestamp)
      payload.attention = attention("approval", "Нужно подтверждение в OpenCode", timestamp)
      await emit(sessionID, "agent.attention_requested", payload, timestamp)
      return
    }
    if (["question.asked", "question.v2.asked"].includes(type)) {
      phases.set(sessionID, "waiting_input")
      const payload = basePayload(sessionID, "waiting_input", "Ждёт выбора", timestamp)
      payload.attention = attention("input", "Нужно ответить в OpenCode", timestamp)
      await emit(sessionID, "agent.attention_requested", payload, timestamp)
      return
    }
    if (["permission.replied", "permission.v2.replied", "question.replied", "question.v2.replied", "question.rejected", "question.v2.rejected"].includes(type)) {
      phases.set(sessionID, "working")
      await emit(sessionID, "agent.attention_resolved", basePayload(sessionID, "working", "Продолжает работу", timestamp), timestamp)
      return
    }
    if (type === "todo.updated") {
      const todos = Array.isArray(properties.todos)
        ? properties.todos.filter((todo) => todo?.status !== "cancelled")
        : []
      if (!todos.length) return
      const completed = todos.filter((todo) => todo?.status === "completed").length
      const payload = basePayload(sessionID, "working", "Выполняет план", timestamp)
      payload.progress = {
        kind: "steps",
        current: completed,
        total: todos.length,
        label: `План: ${completed} из ${todos.length}`,
        source: "explicit",
      }
      phases.set(sessionID, "working")
      await emit(sessionID, "agent.progress", payload, timestamp)
      return
    }
    if (type === "session.diff") {
      const summary = changeSummary(properties.diff)
      if (!summary) return
      changeSummaries.set(sessionID, summary)
      const payload = basePayload(sessionID, "working", "Изменяет файлы", timestamp)
      phases.set(sessionID, "working")
      await emit(sessionID, "agent.activity", payload, timestamp)
    }
  }

  async function toolBefore(input) {
    const timestamp = isoTime(now())
    await working(input?.sessionID, genericToolAction(input?.tool), timestamp)
  }

  async function toolAfter(input) {
    const timestamp = isoTime(now())
    await working(input?.sessionID, "Проверяет результат", timestamp)
  }

  async function bootstrap(client) {
    try {
      const listed = unwrapData(await client?.session?.list?.({
        query: {
          directory: "",
          roots: true,
          limit: MAX_BOOTSTRAP_SESSIONS,
        },
      }))
      const sessions = (Array.isArray(listed) ? listed : [])
        .filter((info) => info && typeof info.id === "string")
        .sort((left, right) => {
          const leftUpdated = Number(left?.time?.updated) || 0
          const rightUpdated = Number(right?.time?.updated) || 0
          return rightUpdated - leftUpdated
        })
        .slice(0, MAX_BOOTSTRAP_SESSIONS)
      for (const info of sessions) remember(info)
      const statuses = unwrapData(await client?.session?.status?.())
      if (!statuses || typeof statuses !== "object") return
      for (const [sessionID, status] of Object.entries(statuses)) {
        if (status?.type === "busy" || status?.type === "retry") {
          await working(sessionID, status.type === "retry" ? "Повторяет попытку" : "Работает над задачей", isoTime(now()), true)
        }
      }
      for (const info of sessions) {
        if (typeof info.parentID === "string" && info.parentID) continue
        const statusType = statuses[info.id]?.type
        if (statusType && statusType !== "idle") continue
        const recovered = await recoverActiveRoot(info.id)
        if (!recovered) continue
        activeTurns.set(info.id, {
          agentId: rootAgentId(info.id),
          startedAt: recovered.startedAt,
        })
        phases.set(info.id, "working")
        const receipt = await recoverAssistantReceipt(info.id, recovered.startedAt, client)
        if (!receipt) continue
        assistantReceipts.set(info.id, receipt)
        await refreshFinalDiff(info.id)
        await terminal(
          info.id,
          "completed",
          "Закончил работу",
          "success",
          "agent.completed",
          receipt.completedAt,
          receipt,
        )
      }
    } catch {
      // Startup recovery is optional; live hooks remain available.
    }
  }

  return { processEvent, toolBefore, toolAfter, bootstrap, remember, refreshFinalDiff }
}

function createRuntime(options = {}) {
  const root = dataRoot(options.dataDir)
  const now = options.now ?? (() => new Date())
  const uuid = options.uuid ?? randomUUID
  const fetchImpl = options.fetchImpl ?? globalThis.fetch
  let queue = Promise.resolve()

  async function deliver(baseEvent) {
    queue = queue.then(async () => {
      const { sequence: _ignored, ...eventWithoutSequence } = baseEvent
      const event = await persistEvent(root, eventWithoutSequence, now, uuid)
      if (event && typeof fetchImpl === "function") await postEvent(root, event, fetchImpl)
    }).catch(() => {})
    await queue
  }

  async function recoverActiveRoot(rawSessionId) {
    return readPersistedActiveRoot(root, rawSessionId, now)
  }

  return { deliver, recoverActiveRoot }
}

const PetCrewPlugin = async (context) => {
  const runtime = createRuntime()
  const mapper = makeMapper(context, runtime.deliver, undefined, runtime.recoverActiveRoot)
  // OpenCode initializes plugins while its local server is still bootstrapping. Calling the
  // server-backed SDK synchronously here deadlocks desktop startup. Register hooks first, then let
  // the event loop run startup recovery after the plugin loader has completed.
  const bootstrapTimer = setTimeout(() => {
    void mapper.bootstrap(context.client)
  }, 750)
  bootstrapTimer.unref?.()
  return {
    event: mapper.processEvent,
    "tool.execute.before": mapper.toolBefore,
    "tool.execute.after": mapper.toolAfter,
    dispose: async () => clearTimeout(bootstrapTimer),
  }
}

PetCrewPlugin.__test = Object.freeze({
  cleanText,
  opaqueId,
  projectFromDirectory,
  navigationFromDirectory,
  genericToolAction,
  changeSummary,
  terminalEventId,
  latestCompletedAssistant,
  makeMapper,
  createRuntime,
  postEvent,
  stableRootIdentity,
  recoverableActiveRoot,
  readPersistedActiveRoot,
})

export { PetCrewPlugin }
