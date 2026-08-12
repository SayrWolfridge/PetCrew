import { describe, expect, it } from "vitest";
import {
  densityForCount,
  displayProgress,
  elapsedWorkingLabel,
  selectLiveAgents,
  selectUnreadSubagents,
  selectUnreadTerminalAgents,
  selectVisibleAgents,
  sortAgents,
  teamMood,
  terminalTimeLabel,
} from "./model";
import type { DemoAgent } from "./types";

const baseAgent: DemoAgent = {
  agent_id: "a",
  provider: "codex",
  project: "PetCrew",
  task: "Проверка",
  phase: "working",
  progress: { kind: "indeterminate", source: "inferred" },
  current_action: "Работает",
};

describe("honest progress", () => {
  it("shows a fraction only for a valid explicit plan", () => {
    expect(
      displayProgress({
        ...baseAgent,
        progress: { kind: "steps", current: 4, total: 10, source: "explicit" },
      }),
    ).toBe("4/10");
  });

  it("hides invalid step progress", () => {
    expect(
      displayProgress({
        ...baseAgent,
        progress: { kind: "steps", current: 11, total: 10, source: "explicit" },
      }),
    ).toBeNull();
    expect(displayProgress(baseAgent)).toBeNull();
  });
});

describe("working duration", () => {
  it("formats elapsed time from an explicit turn start", () => {
    const agent = {
      ...baseAgent,
      started_at: "2026-07-19T10:00:00Z",
    };

    expect(elapsedWorkingLabel(agent, Date.parse("2026-07-19T10:00:30Z"))).toBe("меньше минуты");
    expect(elapsedWorkingLabel(agent, Date.parse("2026-07-19T10:17:00Z"))).toBe("17 мин");
    expect(elapsedWorkingLabel(agent, Date.parse("2026-07-19T11:12:00Z"))).toBe("1 ч 12 мин");
  });

  it("omits elapsed time outside working or without a valid start", () => {
    expect(elapsedWorkingLabel(baseAgent, Date.now())).toBeNull();
    expect(
      elapsedWorkingLabel(
        { ...baseAgent, phase: "completed", started_at: "2026-07-19T10:00:00Z" },
        Date.parse("2026-07-19T11:00:00Z"),
      ),
    ).toBeNull();
  });
});

describe("terminal finish time", () => {
  const localIso = (year: number, month: number, day: number, hour: number, minute: number) =>
    new Date(year, month - 1, day, hour, minute).toISOString();

  it("formats today, yesterday and an older retained result", () => {
    const now = new Date(2026, 6, 19, 18, 0).getTime();
    const completed = { ...baseAgent, phase: "completed" as const };
    expect(terminalTimeLabel({ ...completed, updated_at: localIso(2026, 7, 19, 16, 42) }, now)).toBe("в 16:42");
    expect(terminalTimeLabel({ ...completed, updated_at: localIso(2026, 7, 18, 23, 5) }, now)).toBe("вчера в 23:05");
    expect(terminalTimeLabel({ ...completed, updated_at: localIso(2026, 7, 16, 9, 7) }, now)).toBe("16.07 в 09:07");
  });

  it("does not invent a finish time for active or invalid records", () => {
    const now = new Date(2026, 6, 19, 18, 0).getTime();
    expect(terminalTimeLabel({ ...baseAgent, updated_at: localIso(2026, 7, 19, 16, 42) }, now)).toBeNull();
    expect(terminalTimeLabel({ ...baseAgent, phase: "completed", updated_at: "invalid" }, now)).toBeNull();
  });
});

describe("team presentation", () => {
  it("uses compact density for a ten-agent team", () => {
    expect(densityForCount(3)).toBe("detailed");
    expect(densityForCount(10)).toBe("compact");
    expect(densityForCount(11)).toBe("grouped");
  });

  it("surfaces unread results below urgent attention and above ongoing work", () => {
    const ordered = sortAgents([
      { ...baseAgent, agent_id: "read", phase: "completed", unread: false },
      { ...baseAgent, agent_id: "queued", phase: "queued" },
      { ...baseAgent, agent_id: "done", phase: "completed", unread: true },
      { ...baseAgent, agent_id: "work", phase: "working" },
      { ...baseAgent, agent_id: "wait", phase: "waiting_input" },
    ]);
    expect(ordered.map((agent) => agent.agent_id)).toEqual([
      "wait",
      "done",
      "work",
      "queued",
      "read",
    ]);
  });

  it("places the newest attention request first", () => {
    const ordered = sortAgents([
      {
        ...baseAgent,
        agent_id: "older-wait",
        phase: "waiting_input",
        updated_at: "2026-07-18T12:00:00+03:00",
      },
      {
        ...baseAgent,
        agent_id: "newer-block",
        phase: "blocked",
        updated_at: "2026-07-18T12:01:00+03:00",
      },
      {
        ...baseAgent,
        agent_id: "newest-failure",
        phase: "failed",
        unread: true,
        updated_at: "2026-07-18T12:02:00+03:00",
      },
    ]);

    expect(ordered.map((agent) => agent.agent_id)).toEqual([
      "newest-failure",
      "newer-block",
      "older-wait",
    ]);
  });

  it("keeps working cards stable while their streamed content updates", () => {
    const first = sortAgents([
      {
        ...baseAgent,
        key: "codex:session:b",
        agent_id: "b",
        task: "Анализ",
        updated_at: "2026-07-18T12:02:00+03:00",
      },
      {
        ...baseAgent,
        key: "codex:session:a",
        agent_id: "a",
        task: "Проверка",
        updated_at: "2026-07-18T12:01:00+03:00",
      },
    ]);
    const updated = sortAgents([
      {
        ...first[1],
        task: "Теперь раньше по алфавиту",
        current_action: "Обновлённое действие",
        updated_at: "2026-07-18T12:03:00+03:00",
      },
      {
        ...first[0],
        task: "Теперь позже по алфавиту",
        current_action: "Другое обновлённое действие",
        updated_at: "2026-07-18T12:04:00+03:00",
      },
    ]);

    expect(first.map((agent) => agent.key)).toEqual(["codex:session:a", "codex:session:b"]);
    expect(updated.map((agent) => agent.key)).toEqual(["codex:session:a", "codex:session:b"]);
  });

  it("inserts a newly started work card first without moving it on streamed updates", () => {
    const ordered = sortAgents([
      {
        ...baseAgent,
        key: "codex:older",
        agent_id: "older",
        started_at: "2026-07-18T12:00:00+03:00",
        updated_at: "2026-07-18T12:10:00+03:00",
      },
      {
        ...baseAgent,
        key: "opencode:newer",
        agent_id: "newer",
        started_at: "2026-07-18T12:05:00+03:00",
        updated_at: "2026-07-18T12:05:00+03:00",
      },
    ]);
    expect(ordered.map((agent) => agent.agent_id)).toEqual(["newer", "older"]);

    const streamed = sortAgents([
      { ...ordered[1], updated_at: "2026-07-18T12:20:00+03:00", current_action: "Новое действие" },
      { ...ordered[0], updated_at: "2026-07-18T12:06:00+03:00" },
    ]);
    expect(streamed.map((agent) => agent.agent_id)).toEqual(["newer", "older"]);
  });

  it("uses the highest-priority aggregate mood", () => {
    expect(teamMood([{ ...baseAgent, phase: "working" }])).toBe("working");
    expect(teamMood([{ ...baseAgent, phase: "working" }, { ...baseAgent, phase: "blocked" }])).toBe(
      "blocked",
    );
    expect(
      teamMood([{ ...baseAgent, phase: "blocked" }, { ...baseAgent, phase: "waiting_approval" }]),
    ).toBe("attention");
  });
});

describe("agent visibility", () => {
  it("excludes simulator cards from the live source", () => {
    const simulator = { ...baseAgent, agent_id: "demo", provider: "simulator" as const };

    expect(selectLiveAgents([baseAgent, simulator])).toEqual([baseAgent]);
  });

  it("always keeps active and unread agents, then adds the newest acknowledged tail", () => {
    const active = Array.from({ length: 3 }, (_, index) => ({
      ...baseAgent,
      agent_id: `active-${index}`,
    }));
    const unread = {
      ...baseAgent,
      agent_id: "unread",
      phase: "completed" as const,
      unread: true,
      updated_at: "2026-07-18T12:30:00+03:00",
    };
    const acknowledged = Array.from({ length: 15 }, (_, index) => ({
      ...baseAgent,
      agent_id: `read-${index}`,
      phase: "completed" as const,
      unread: false,
      updated_at: `2026-07-18T12:${String(index).padStart(2, "0")}:00+03:00`,
    }));

    const selection = selectVisibleAgents([...active, unread, ...acknowledged], 10);
    const ids = new Set(selection.agents.map((agent) => agent.agent_id));

    expect(selection.agents).toHaveLength(14);
    expect(selection.hiddenRecentCount).toBe(5);
    expect(selection.overflowCount).toBe(0);
    expect(ids.has("unread")).toBe(true);
    expect(ids.has("read-14")).toBe(true);
    expect(ids.has("read-4")).toBe(false);
  });

  it("can hide acknowledged history without hiding protected agents", () => {
    const selection = selectVisibleAgents(
      [
        { ...baseAgent, agent_id: "working" },
        { ...baseAgent, agent_id: "read", phase: "completed", unread: false },
      ],
      0,
    );

    expect(selection.agents.map((agent) => agent.agent_id)).toEqual(["working"]);
    expect(selection.hiddenRecentCount).toBe(1);
  });

  it("allows protected agents to exceed the soft capacity", () => {
    const protectedAgents = Array.from({ length: 101 }, (_, index) => ({
      ...baseAgent,
      agent_id: `active-${index}`,
    }));
    const selection = selectVisibleAgents(protectedAgents, 10, 100);

    expect(selection.agents).toHaveLength(101);
    expect(selection.overflowCount).toBe(1);
  });
});

describe("bulk acknowledgement targets", () => {
  it("selects every unread terminal result and leaves active or read cards alone", () => {
    const agents: DemoAgent[] = [
      { ...baseAgent, agent_id: "root-done", phase: "completed", unread: true },
      { ...baseAgent, agent_id: "child-error", phase: "failed", unread: true, parent_agent_id: "root-done" },
      { ...baseAgent, agent_id: "child-read", phase: "completed", unread: false, parent_agent_id: "root-done" },
      { ...baseAgent, agent_id: "child-working", phase: "working", unread: true, parent_agent_id: "root-done" },
    ];

    expect(selectUnreadTerminalAgents(agents).map((agent) => agent.agent_id)).toEqual([
      "root-done",
      "child-error",
    ]);
  });

  it("limits the subagent action to unread terminal descendants", () => {
    const agents: DemoAgent[] = [
      { ...baseAgent, agent_id: "root-done", phase: "completed", unread: true },
      { ...baseAgent, agent_id: "child-done", phase: "cancelled", unread: true, parent_agent_id: "root-done" },
      { ...baseAgent, agent_id: "child-waiting", phase: "waiting_input", unread: true, parent_agent_id: "root-done" },
      { ...baseAgent, agent_id: "child-read", phase: "failed", unread: false, parent_agent_id: "root-done" },
    ];

    expect(selectUnreadSubagents(agents).map((agent) => agent.agent_id)).toEqual(["child-done"]);
  });
});
