import { describe, expect, it } from "vitest";
import fixture from "../../../tests/fixtures/demo-team.json";
import { createAdvanceHubEvents, createInitialHubEvents } from "./hub";
import type { DemoAgent } from "./types";

const team = fixture as DemoAgent[];

describe("local hub demo writer", () => {
  it("creates one authenticated-transport envelope per visible fixture agent", () => {
    const events = createInitialHubEvents(team, "run-1");

    expect(events).toHaveLength(10);
    expect(events.every((event) => event.protocol_version === "1.0")).toBe(true);
    expect(events.every((event) => event.provider === "simulator")).toBe(true);
    expect(new Set(events.map((event) => event.event_id)).size).toBe(10);
  });

  it("keeps explicit fractions and never adds them to indeterminate progress", () => {
    const events = createInitialHubEvents(team, "run-2");
    const explicit = events.find((event) => event.agent_id === "demo:codex-research");
    const indeterminate = events.find((event) => event.agent_id === "demo:codex-copy");

    expect(explicit?.payload.progress).toMatchObject({
      kind: "steps",
      current: 4,
      total: 10,
      source: "explicit",
    });
    expect(indeterminate?.payload.progress).not.toHaveProperty("current");
    expect(indeterminate?.payload.progress).not.toHaveProperty("total");
  });

  it("transmits the unread completion result used by the acknowledgement action", () => {
    const events = createInitialHubEvents(team, "run-result");
    const completed = events.find(
      (event) => event.agent_id === "demo:codex-architecture",
    );

    expect(completed?.event_type).toBe("agent.completed");
    expect(completed?.payload.result).toMatchObject({
      summary: "Архитектура и протокол событий подготовлены",
      outcome: "success",
      unread: true,
    });
  });

  it("advances each agent from its own last accepted sequence", () => {
    const liveAgent: DemoAgent = {
      ...team[0],
      key: "simulator:demo:agent",
      session_id: "demo",
      agent_id: "agent",
      last_sequence: 7,
    };

    const [event] = createAdvanceHubEvents([liveAgent], "run-3", 1);

    expect(event.sequence).toBe(8);
    expect(event.payload.progress).toMatchObject({ current: 5, total: 10 });
  });

  it("does not replay event ids when the restored step counter starts over", () => {
    const restoredAgent: DemoAgent = {
      ...team[6],
      key: "simulator:restored:agent",
      session_id: "restored-session",
      agent_id: "restored-agent",
      phase: "working",
      last_sequence: 7,
    };

    const [beforeRestart] = createAdvanceHubEvents([restoredAgent], "restored", 1);
    const [afterRestart] = createAdvanceHubEvents(
      [{ ...restoredAgent, last_sequence: beforeRestart.sequence }],
      "restored",
      1,
    );

    expect(beforeRestart.sequence).toBe(8);
    expect(afterRestart.sequence).toBe(9);
    expect(afterRestart.event_id).not.toBe(beforeRestart.event_id);
  });
});
