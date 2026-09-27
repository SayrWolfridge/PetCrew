import { describe, expect, it } from "vitest";
import {
  emptyListDisclosure,
  parseListDisclosure,
  serializeListDisclosure,
  toggleCardDisclosure,
  toggleProjectDisclosure,
} from "./list-disclosure";
import type { DemoAgent } from "./types";

const agent: DemoAgent = {
  key: "codex:session:agent-a",
  agent_id: "agent-a",
  provider: "codex",
  project: "PetCrew",
  task: "Проверить группировку",
  phase: "working",
  progress: { kind: "indeterminate", source: "inferred" },
  current_action: "Проверяет список",
};

describe("list disclosure persistence", () => {
  it("starts with projects open and cards compact", () => {
    const state = emptyListDisclosure();
    expect([...state.collapsedProjects]).toEqual([]);
    expect([...state.expandedCards]).toEqual([]);
  });

  it("toggles project and card independently", () => {
    const collapsed = toggleProjectDisclosure(emptyListDisclosure(), "PetCrew");
    const withExpandedCard = toggleCardDisclosure(collapsed, agent);

    expect(withExpandedCard.collapsedProjects.has("PetCrew")).toBe(true);
    expect(withExpandedCard.expandedCards.has(agent.key!)).toBe(true);

    const reopened = toggleProjectDisclosure(withExpandedCard, "PetCrew");
    expect(reopened.collapsedProjects.has("PetCrew")).toBe(false);
    expect(reopened.expandedCards.has(agent.key!)).toBe(true);
  });

  it("round-trips the versioned minimal storage shape", () => {
    const state = toggleCardDisclosure(
      toggleProjectDisclosure(emptyListDisclosure(), "Sayr"),
      agent,
    );
    const restored = parseListDisclosure(serializeListDisclosure(state));

    expect([...restored.collapsedProjects]).toEqual(["Sayr"]);
    expect([...restored.expandedCards]).toEqual([agent.key]);
  });

  it("ignores malformed and unknown storage versions", () => {
    expect(parseListDisclosure("not json")).toEqual(emptyListDisclosure());
    expect(parseListDisclosure('{"schema_version":2,"collapsed_projects":["PetCrew"]}'))
      .toEqual(emptyListDisclosure());
  });
});
