import type { AgentPhase, DemoAgent, Density, TeamMood } from "./types";

export const PHASE_LABELS: Record<AgentPhase, string> = {
  queued: "В очереди",
  planning: "Планирует",
  working: "Работает",
  waiting_input: "Нужен ответ",
  waiting_approval: "Нужно разрешение",
  blocked: "Заблокирован",
  completed: "Готово",
  failed: "Ошибка",
  cancelled: "Остановлен",
};

export const DEFAULT_RECENT_COMPLETED = 10;
export const MAX_RETAINED_AGENTS = 100;

const TERMINAL_PHASES = new Set<AgentPhase>(["completed", "failed", "cancelled"]);

export interface AgentSelection {
  agents: DemoAgent[];
  hiddenRecentCount: number;
  overflowCount: number;
}

function isTerminal(agent: DemoAgent): boolean {
  return TERMINAL_PHASES.has(agent.phase);
}

export function isUnreadTerminal(agent: DemoAgent): boolean {
  return isTerminal(agent) && Boolean(agent.unread);
}

export function selectUnreadTerminalAgents(agents: DemoAgent[]): DemoAgent[] {
  return agents.filter(isUnreadTerminal);
}

export function selectUnreadSubagents(agents: DemoAgent[]): DemoAgent[] {
  return selectUnreadTerminalAgents(agents).filter((agent) => Boolean(agent.parent_agent_id));
}

function isProtected(agent: DemoAgent): boolean {
  return !isTerminal(agent) || Boolean(agent.unread);
}

function updatedAtMillis(agent: DemoAgent): number {
  const timestamp = Date.parse(agent.updated_at ?? "");
  return Number.isFinite(timestamp) ? timestamp : 0;
}

function startedAtMillis(agent: DemoAgent): number | null {
  const timestamp = Date.parse(agent.started_at ?? "");
  return Number.isFinite(timestamp) ? timestamp : null;
}

function requiresAttention(agent: DemoAgent): boolean {
  return (
    agent.phase === "waiting_approval" ||
    agent.phase === "waiting_input" ||
    agent.phase === "blocked" ||
    (agent.phase === "failed" && Boolean(agent.unread))
  );
}

function stableIdentity(agent: DemoAgent): string {
  return agent.key ?? agent.agent_id;
}

function phasePriority(agent: DemoAgent): number {
  if (requiresAttention(agent)) return 0;
  if (isTerminal(agent) && agent.unread) return 1;
  if (agent.phase === "working" || agent.phase === "planning") return 2;
  if (agent.phase === "queued") return 3;
  return 4;
}

export function displayProgress(agent: DemoAgent): string | null {
  const progress = agent.progress;
  if (
    progress.kind !== "steps" ||
    progress.source !== "explicit" ||
    !Number.isInteger(progress.current) ||
    !Number.isInteger(progress.total) ||
    progress.total <= 0 ||
    progress.current < 0 ||
    progress.current > progress.total
  ) {
    return null;
  }

  return `${progress.current}/${progress.total}`;
}

export function progressRatio(agent: DemoAgent): number | null {
  if (!displayProgress(agent) || agent.progress.kind !== "steps") return null;
  return agent.progress.current / agent.progress.total;
}

export function elapsedWorkingLabel(agent: DemoAgent, nowMillis: number): string | null {
  if (agent.phase !== "working" || !agent.started_at) return null;
  const startedAt = Date.parse(agent.started_at);
  if (!Number.isFinite(startedAt) || startedAt > nowMillis) return null;
  const elapsedMinutes = Math.floor((nowMillis - startedAt) / 60_000);
  if (elapsedMinutes < 1) return "меньше минуты";
  if (elapsedMinutes < 60) return `${elapsedMinutes} мин`;
  const hours = Math.floor(elapsedMinutes / 60);
  const minutes = elapsedMinutes % 60;
  return minutes === 0 ? `${hours} ч` : `${hours} ч ${minutes} мин`;
}

function sameLocalDay(left: Date, right: Date): boolean {
  return left.getFullYear() === right.getFullYear()
    && left.getMonth() === right.getMonth()
    && left.getDate() === right.getDate();
}

function twoDigits(value: number): string {
  return String(value).padStart(2, "0");
}

export function terminalTimeLabel(agent: DemoAgent, nowMillis: number): string | null {
  if (!isTerminal(agent) || !agent.updated_at) return null;
  const finishedAtMillis = Date.parse(agent.updated_at);
  if (!Number.isFinite(finishedAtMillis) || finishedAtMillis > nowMillis + 5 * 60_000) return null;
  const finishedAt = new Date(finishedAtMillis);
  const now = new Date(nowMillis);
  const time = `${twoDigits(finishedAt.getHours())}:${twoDigits(finishedAt.getMinutes())}`;
  if (sameLocalDay(finishedAt, now)) return `в ${time}`;

  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  if (sameLocalDay(finishedAt, yesterday)) return `вчера в ${time}`;
  return `${twoDigits(finishedAt.getDate())}.${twoDigits(finishedAt.getMonth() + 1)} в ${time}`;
}

export function densityForCount(count: number): Density {
  if (count <= 3) return "detailed";
  if (count <= 10) return "compact";
  return "grouped";
}

export function sortAgents(agents: DemoAgent[]): DemoAgent[] {
  return [...agents].sort((left, right) => {
    const priority = phasePriority(left) - phasePriority(right);
    if (priority !== 0) return priority;

    if (requiresAttention(left) && requiresAttention(right)) {
      const newestFirst = updatedAtMillis(right) - updatedAtMillis(left);
      if (newestFirst !== 0) return newestFirst;
    }

    if (isTerminal(left) && isTerminal(right)) {
      const newestFirst = updatedAtMillis(right) - updatedAtMillis(left);
      if (newestFirst !== 0) return newestFirst;
    }

    if (
      (left.phase === "working" || left.phase === "planning")
      && (right.phase === "working" || right.phase === "planning")
    ) {
      const leftStarted = startedAtMillis(left);
      const rightStarted = startedAtMillis(right);
      if (leftStarted !== null && rightStarted !== null && leftStarted !== rightStarted) {
        return rightStarted - leftStarted;
      }
      if (leftStarted !== null && rightStarted === null) return -1;
      if (leftStarted === null && rightStarted !== null) return 1;
    }

    return stableIdentity(left).localeCompare(stableIdentity(right), "ru");
  });
}

export function selectVisibleAgents(
  agents: DemoAgent[],
  recentCompletedLimit = DEFAULT_RECENT_COMPLETED,
  capacity = MAX_RETAINED_AGENTS,
): AgentSelection {
  const protectedAgents = agents.filter(isProtected);
  const acknowledgedTerminal = agents
    .filter((agent) => isTerminal(agent) && !agent.unread)
    .sort((left, right) => {
      const newestFirst = updatedAtMillis(right) - updatedAtMillis(left);
      if (newestFirst !== 0) return newestFirst;
      return (left.key ?? left.agent_id).localeCompare(right.key ?? right.agent_id, "ru");
    });
  const normalizedCapacity = Math.max(0, Math.floor(capacity));
  const normalizedLimit = Math.max(0, Math.floor(recentCompletedLimit));
  const availableHistorySlots = Math.max(0, normalizedCapacity - protectedAgents.length);
  const acknowledgedToShow = acknowledgedTerminal.slice(
    0,
    Math.min(normalizedLimit, availableHistorySlots),
  );

  return {
    agents: sortAgents([...protectedAgents, ...acknowledgedToShow]),
    hiddenRecentCount: acknowledgedTerminal.length - acknowledgedToShow.length,
    overflowCount: Math.max(0, protectedAgents.length - normalizedCapacity),
  };
}

export function selectLiveAgents(agents: DemoAgent[]): DemoAgent[] {
  return agents.filter((agent) => agent.provider !== "simulator");
}

export function teamMood(agents: DemoAgent[]): TeamMood {
  if (agents.some((agent) => agent.phase === "waiting_input" || agent.phase === "waiting_approval")) {
    return "attention";
  }
  if (agents.some((agent) => agent.phase === "blocked" || (agent.phase === "failed" && agent.unread))) {
    return "blocked";
  }
  if (agents.some((agent) => agent.phase === "completed" && agent.unread)) {
    return "success";
  }
  if (agents.some((agent) => agent.phase === "working" || agent.phase === "planning")) {
    return "working";
  }
  return "idle";
}

export function teamCounts(agents: DemoAgent[]) {
  return agents.reduce(
    (counts, agent) => {
      if (agent.phase === "working" || agent.phase === "planning") counts.working += 1;
      if (agent.phase === "waiting_input" || agent.phase === "waiting_approval") counts.waiting += 1;
      if (agent.phase === "completed") counts.done += 1;
      if (agent.phase === "blocked" || (agent.phase === "failed" && agent.unread)) counts.blocked += 1;
      return counts;
    },
    { working: 0, waiting: 0, done: 0, blocked: 0 },
  );
}

export function advanceSimulation(agents: DemoAgent[], tick: number): DemoAgent[] {
  return agents.map((agent, index) => {
    if (agent.phase === "planning" && tick % 2 === 0) {
      return { ...agent, phase: "working", current_action: "Выполняет первый шаг плана" };
    }

    if (agent.phase === "queued" && (tick + index) % 5 === 0) {
      return { ...agent, phase: "planning", current_action: "Готовит план работы" };
    }

    if (agent.phase !== "working" || agent.progress.kind !== "steps") return agent;

    const current = Math.min(agent.progress.current + 1, agent.progress.total);
    if (current === agent.progress.total) {
      return {
        ...agent,
        phase: "completed",
        progress: { ...agent.progress, current },
        current_action: "Работа завершена",
        result: `Закончил: ${agent.task.toLocaleLowerCase("ru")}`,
        unread: true,
      };
    }

    return {
      ...agent,
      progress: { ...agent.progress, current },
      current_action: `Выполняет шаг ${current} из ${agent.progress.total}`,
    };
  });
}
