import { invoke } from "@tauri-apps/api/core";
import type {
  AgentPhase,
  AgentProgress,
  ChangeSummary,
  DemoAgent,
  HubConnection,
  HubSnapshot,
  Provider,
} from "./types";

interface EventPayload {
  project?: { id: string; name: string; path?: string | null };
  task?: { title: string; detail?: string | null };
  phase?: AgentPhase;
  progress?: {
    kind: AgentProgress["kind"];
    current?: number;
    total?: number;
    label: string;
    source: AgentProgress["source"];
  };
  change_summary?: ChangeSummary;
  current_action?: string;
  attention?: {
    kind: "input" | "approval" | "blocked" | "failure";
    summary: string;
    requested_at: string;
  };
  result?: {
    summary: string;
    outcome: "success" | "failure" | "cancelled";
    completed_at: string;
    unread: boolean;
  };
}

export interface AgentEventEnvelope {
  protocol_version: "1.0";
  event_id: string;
  sequence: number;
  occurred_at: string;
  provider: Provider;
  session_id: string;
  agent_id: string;
  parent_agent_id: string | null;
  event_type:
    | "agent.discovered"
    | "agent.started"
    | "agent.progress"
    | "agent.activity"
    | "agent.attention_requested"
    | "agent.attention_resolved"
    | "agent.completed"
    | "agent.failed"
    | "agent.cancelled";
  payload: EventPayload;
}

export function getHubConnection() {
  return invoke<HubConnection>("get_hub_connection");
}

function authorizedHeaders(connection: HubConnection) {
  return { Authorization: `Bearer ${connection.token}` };
}

export async function getHubSnapshot(connection: HubConnection) {
  const response = await fetch(`${connection.endpoint}/v1/snapshot`, {
    headers: authorizedHeaders(connection),
  });
  if (!response.ok) throw new Error(`hub_snapshot_http_${response.status}`);
  return (await response.json()) as HubSnapshot;
}

function snapshotsFromSseChunk(
  buffer: string,
  listener: (snapshot: HubSnapshot) => void,
): { remainder: string; revision?: number } {
  const blocks = buffer.split(/\r?\n\r?\n/);
  const remainder = blocks.pop() ?? "";
  let revision: number | undefined;
  for (const block of blocks) {
    const lines = block.split(/\r?\n/);
    if (!lines.some((line) => line === "event: snapshot")) continue;
    const data = lines
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (!data) continue;
    const snapshot = JSON.parse(data) as HubSnapshot;
    revision = Math.max(revision ?? 0, snapshot.revision);
    listener(snapshot);
  }
  return { remainder, revision };
}

export async function subscribeToHub(
  connection: HubConnection,
  initialRevision: number,
  listener: (snapshot: HubSnapshot) => void,
) {
  const controller = new AbortController();
  let revision = initialRevision;

  void (async () => {
    while (!controller.signal.aborted) {
      try {
        const response = await fetch(
          `${connection.endpoint}/v1/snapshots/stream?after=${revision}`,
          {
            headers: authorizedHeaders(connection),
            signal: controller.signal,
          },
        );
        if (!response.ok || !response.body) {
          throw new Error(`hub_snapshot_stream_http_${response.status}`);
        }
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (!controller.signal.aborted) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const parsed = snapshotsFromSseChunk(buffer, listener);
          buffer = parsed.remainder;
          revision = Math.max(revision, parsed.revision ?? revision);
        }
      } catch (error) {
        if (controller.signal.aborted) break;
        console.error("Поток снимков PetCrew временно недоступен", error);
      }
      await new Promise<void>((resolve) => window.setTimeout(resolve, 1000));
    }
  })();

  return () => controller.abort();
}

export async function acknowledgeHubAgent(connection: HubConnection, key: string) {
  const response = await fetch(`${connection.endpoint}/v1/acknowledgements`, {
    method: "POST",
    headers: {
      ...authorizedHeaders(connection),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ key }),
  });
  if (!response.ok) throw new Error(`hub_acknowledgement_http_${response.status}`);
  return (await response.json()) as HubSnapshot;
}

export function openCodexThread(threadId: string) {
  return invoke<void>("open_codex_thread", { threadId });
}

export function openOpenCodeProject(directory: string) {
  return invoke<void>("open_opencode_project", { directory });
}

export async function sendHubEvent(connection: HubConnection, event: AgentEventEnvelope) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 1800);
  try {
    const response = await fetch(`${connection.endpoint}/v1/events`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${connection.token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(event),
      signal: controller.signal,
    });
    if (!response.ok) {
      const errorBody = (await response.json().catch(() => null)) as { error?: string } | null;
      throw new Error(errorBody?.error ?? `hub_http_${response.status}`);
    }
  } finally {
    window.clearTimeout(timeout);
  }
}

function eventProgress(agent: DemoAgent) {
  return {
    ...agent.progress,
    label: agent.current_action || "Выполняет работу",
  };
}

function demoEventType(agent: DemoAgent): AgentEventEnvelope["event_type"] {
  if (agent.phase === "completed") return "agent.completed";
  if (agent.phase === "failed") return "agent.failed";
  if (agent.phase === "cancelled") return "agent.cancelled";
  if (
    agent.phase === "waiting_input" ||
    agent.phase === "waiting_approval" ||
    agent.phase === "blocked"
  ) {
    return "agent.attention_requested";
  }
  if (agent.phase === "queued") return "agent.discovered";
  return "agent.started";
}

function attentionKind(agent: DemoAgent): "input" | "approval" | "blocked" | "failure" {
  if (agent.phase === "waiting_approval") return "approval";
  if (agent.phase === "blocked") return "blocked";
  if (agent.phase === "failed") return "failure";
  return "input";
}

export function createInitialHubEvents(agents: DemoAgent[], runId: string): AgentEventEnvelope[] {
  return agents.map((agent, index) => {
    const occurredAt = new Date(Date.now() + index).toISOString();
    const eventType = demoEventType(agent);
    const payload: EventPayload = {
      project: { id: "petcrew-demo", name: "PetCrew · local hub" },
      task: { title: agent.task },
      phase: agent.phase,
      progress: eventProgress(agent),
      current_action: agent.current_action || agent.result || "Ожидает обновления",
    };

    if (eventType === "agent.attention_requested") {
      payload.attention = {
        kind: attentionKind(agent),
        summary: agent.current_action || "Требуется внимание",
        requested_at: occurredAt,
      };
    }
    if (
      eventType === "agent.completed" ||
      eventType === "agent.failed" ||
      eventType === "agent.cancelled"
    ) {
      payload.result = {
        summary: agent.result || "Сценарий завершён",
        outcome:
          eventType === "agent.completed"
            ? "success"
            : eventType === "agent.failed"
              ? "failure"
              : "cancelled",
        completed_at: occurredAt,
        unread: Boolean(agent.unread),
      };
    }

    return {
      protocol_version: "1.0",
      event_id: `${runId}:initial:${index}`,
      sequence: 1,
      occurred_at: occurredAt,
      provider: "simulator",
      session_id: `local-hub-demo:${runId}`,
      agent_id: `demo:${agent.agent_id}`,
      parent_agent_id: null,
      event_type: eventType,
      payload,
    };
  });
}

export function createAdvanceHubEvents(
  agents: DemoAgent[],
  runId: string,
  stepId: number,
): AgentEventEnvelope[] {
  return agents.flatMap((agent, index) => {
    if (["completed", "failed", "cancelled"].includes(agent.phase)) return [];

    const occurredAt = new Date(Date.now() + index).toISOString();
    const sequence = (agent.last_sequence ?? 0) + 1;
    let eventType: AgentEventEnvelope["event_type"] = "agent.activity";
    let phase: AgentPhase = "working";
    let currentAction = "Продолжает работу через local hub";
    let progress = agent.progress;
    let result: EventPayload["result"];

    if (agent.progress.kind === "steps" && agent.phase === "working") {
      const current = Math.min(agent.progress.current + 1, agent.progress.total);
      progress = { ...agent.progress, current };
      currentAction = `Выполняет шаг ${current} из ${agent.progress.total}`;
      eventType = "agent.progress";
      if (current === agent.progress.total) {
        eventType = "agent.completed";
        phase = "completed";
        result = {
          summary: `Закончил: ${agent.task.toLocaleLowerCase("ru")}`,
          outcome: "success",
          completed_at: occurredAt,
          unread: true,
        };
      }
    } else if (["waiting_input", "waiting_approval", "blocked"].includes(agent.phase)) {
      eventType = "agent.attention_resolved";
      currentAction = "Внимание обработано, продолжает работу";
    } else if (agent.phase === "queued" || agent.phase === "planning") {
      eventType = "agent.started";
      currentAction = "Начал выполнять задачу";
    }

    return [
      {
        protocol_version: "1.0",
        event_id: `${runId}:sequence:${sequence}:step:${stepId}:${agent.agent_id}`,
        sequence,
        occurred_at: occurredAt,
        provider: "simulator",
        session_id: agent.session_id || `local-hub-demo:${runId}`,
        agent_id: agent.agent_id,
        parent_agent_id: agent.parent_agent_id ?? null,
        event_type: eventType,
        payload: {
          phase,
          progress: { ...progress, label: currentAction },
          current_action: currentAction,
          result,
        },
      },
    ];
  });
}
